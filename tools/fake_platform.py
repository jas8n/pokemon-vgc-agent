"""Play the real agent (agent/agent.py) through a fake platform built from our engine.

Emits observations and templates in the documented platform format (docs/pokemon-skill.md),
in poke-env-like spelling, so the adapter's parsing and the action round trip get exercised:
draft -> team preview -> doubles battle (with forced switches) against a baseline opponent.

    PYTHONPATH=. .venv/bin/python tools/fake_platform.py --matches 3
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from agent.agent import PokemonAgent
from altruagent import DecisionContext, GameState, WithReasoning
from pokeagent import arena
from pokeagent.dex import species, to_id
from pokeagent.engine import move_data, resolve, slot_options, start_battle
from pokeagent.model import Build, Field, Mon, Side, State
from pokeagent.sample_cards import SAMPLE_CARDS
from pokeagent.sim import policy_greedy, replace_best

ME, OPP = "agent-me", "agent-opp"
STATUS_OUT = {"brn": "BRN", "par": "PAR", "psn": "PSN", "tox": "TOX", "slp": "SLP", "frz": "FRZ"}


def gs(phase: str, obs: dict, actions: list[dict], version: int) -> GameState:
    return GameState.from_mcp_state({
        "session_id": "fake-session", "game_type": "pokemon_vgc_doubles_draft", "status": "in_progress",
        "state_version": version, "phase": phase, "observation": obs, "is_terminal": False,
        "is_current_actor": True, "legal_actions": {"state_version": version, "actions": actions},
    })


CTX = DecisionContext(session_id="fake-session", tournament_id=None, game_type="pokemon_vgc_doubles_draft",
                      agent_id=ME, seat_position=0)


def unwrap(decision):
    if isinstance(decision, WithReasoning):
        return decision.action
    return decision


def summary(mon: Mon, opponent: bool, active: bool) -> dict:
    entry = species(mon.build.name)
    return {
        "species": mon.sid, "name": mon.name,
        "current_hp": (round(100 * mon.hp_frac) if opponent else mon.hp),
        "max_hp": 100 if opponent else mon.maxhp,
        "current_hp_fraction": round(mon.hp_frac, 4),
        "status": "FNT" if mon.fainted else STATUS_OUT.get(mon.status),
        "ability": mon.ability, "item": mon.item, "types": [t.upper() for t in mon.types],
        "base_stats": entry["baseStats"], "boosts": dict(mon.boosts),
        "moves": {m: {} for m in mon.build.moves}, "fainted": mon.fainted, "active": active,
        "revealed": mon.revealed,
    }


def battle_obs(st: State, side: int) -> dict:
    me, opp = st.sides[side], st.sides[1 - side]
    f = st.field
    weather = {}
    if f.weather:
        name = {"sun": "SUNNYDAY", "rain": "RAINDANCE", "sand": "SANDSTORM", "snow": "SNOWSCAPE"}[f.weather]
        weather[f"Weather.{name}"] = st.turn - (5 - f.weather_turns)
    fields = {}
    if f.terrain:
        fields[f"Field.{f.terrain.upper()}_TERRAIN"] = st.turn - (5 - f.terrain_turns)
    if f.trickroom:
        fields["Field.TRICK_ROOM"] = st.turn - (5 - f.trickroom)

    def conds(sd):
        out = {}
        if sd.tailwind:
            out["SideCondition.TAILWIND"] = st.turn - (4 - sd.tailwind)
        if sd.reflect:
            out["SideCondition.REFLECT"] = st.turn - (5 - sd.reflect)
        if sd.lightscreen:
            out["SideCondition.LIGHT_SCREEN"] = st.turn - (5 - sd.lightscreen)
        return out

    return {
        "turn": st.turn, "is_doubles": True, "format": "gen9vgc2025regi",
        "weather": weather, "fields": fields,
        "active_pokemon": [summary(me.mons[i], False, True) if i is not None and me.mons[i].alive else None
                           for i in me.active],
        "opponent_active_pokemon": [summary(opp.mons[i], True, True) if i is not None and opp.mons[i].alive else None
                                    for i in opp.active],
        "team": {f"p{side + 1}: {m.name}": summary(m, False, i in me.active) for i, m in enumerate(me.mons)},
        "opponent_team": {f"p{2 - side}: {m.name}": summary(m, True, i in opp.active)
                          for i, m in enumerate(opp.mons) if m.revealed},
        "side_conditions": conds(me), "opponent_side_conditions": conds(opp),
    }


def doubles_template(st: State, side: int, forced: list[bool] | None = None) -> dict:
    sd = st.sides[side]
    slots = []
    for slot in (0, 1):
        mon = sd.active_mon(slot)
        options = []
        if forced is not None:
            if forced[slot]:
                options = [{"type": "switch", "species": sd.mons[i].sid} for i in sd.bench()]
            options.append({"type": "pass"}) if not forced[slot] or not options else None
        elif mon is None:
            options = [{"type": "pass"}]
        else:
            for a in slot_options(st, side, slot):
                if a[0] == "switch":
                    options.append({"type": "switch", "species": sd.mons[a[1]].sid})
                elif a[0] == "move":
                    if any(o.get("move_id") == a[1] for o in options):
                        continue
                    md = move_data(a[1])
                    kind = md.get("target")
                    targets, tops = [], []
                    if kind in ("normal", "any", "adjacentFoe"):
                        for t in (1, 2):
                            foe = st.sides[1 - side].active_mon(t - 1)
                            if foe is not None:
                                targets.append(t)
                                tops.append({"target": t, "side": "opponent", "species": foe.sid})
                        ally = sd.active_mon(1 - slot)
                        if ally is not None:
                            targets.append(-(2 - slot))
                            tops.append({"target": -(2 - slot), "side": "ally", "species": ally.sid})
                    elif kind == "adjacentAllyOrSelf":
                        targets = [-(slot + 1)]
                        tops = [{"target": -(slot + 1), "side": "self", "species": mon.sid}]
                    options.append({"type": "move", "move_id": a[1], "base_power": md.get("basePower"),
                                    "category": md["category"].upper(), "move_type": md["type"].upper(),
                                    "targets": targets, "target_options": tops})
        slots.append({"slot": slot, "board_position": -(slot + 1),
                      "active": {"species": mon.sid} if mon else None,
                      "force_switch": bool(forced and forced[slot]), "options": options})
    return {"type": "doubles_turn", "slots": slots, "target_legend": {"1": "opp A", "2": "opp B"},
            "instructions": "Submit one action per slot."}


def to_engine(sd: Side, choice: dict) -> list:
    out = []
    for n in (0, 1):
        c = choice.get(f"slot_{n}") or {"type": "pass"}
        if c["type"] == "move":
            out.append(("move", to_id(c["move_id"]), c.get("target", 0)))
        elif c["type"] == "switch":
            out.append(("switch", next(i for i, m in enumerate(sd.mons) if m.sid == to_id(c["species"]))))
        else:
            out.append(("pass",))
    return out


def run_match(rng: random.Random, verbose: bool = False) -> int:
    agent = PokemonAgent()
    agent.cards = {}
    pool = rng.sample(SAMPLE_CARDS, 18)
    rosters = {ME: [], OPP: []}
    available = list(pool)
    naive = arena.AGENTS["naive"]
    for n, turn in enumerate(arena.SNAKE):
        seat = ME if turn == "A" else OPP
        items = {to_id(c["item"]) for c in rosters[seat]}
        offer = [c for c in available if to_id(c["item"]) not in items]
        if seat == ME:
            obs = {"phase": "draft", "pick_number": n + 1, "available_cards": available,
                   "available_card_ids": [c["card_id"] for c in available],
                   "rosters": {k: [{"card_id": c["card_id"], "species": c["species"]} for c in v] for k, v in rosters.items()}}
            acts = [{"action_id": f"draft_pick:{c['card_id']}", "label": c["species"],
                     "input": {"action": {"type": "draft_pick", "card_id": c["card_id"]}}} for c in offer]
            pick = unwrap(agent.choose_action(gs("draft", obs, acts, n), CTX))
            pick_id = pick["card_id"]
        else:
            pick_id = naive.draft(offer, rosters[OPP], rosters[ME], pool, rng)
        card = next(c for c in offer if c["card_id"] == pick_id)
        rosters[seat].append(card)
        available.remove(card)
    if verbose:
        print("mine:", [c["species"] for c in rosters[ME]], "\ntheirs:", [c["species"] for c in rosters[OPP]])

    my_b = [Build.from_card(c) for c in rosters[ME]]
    op_b = [Build.from_card(c) for c in rosters[OPP]]
    preview = {"phase": "team_preview", "battle_format": "gen9vgc2025regi", "waiting_for_action": True,
               "your_roster": [{"species": b.sid, "name": b.name, "types": [t.upper() for t in b.types],
                                "base_stats": b.base} for b in my_b],
               "opponent_roster": [{"species": b.sid, "name": b.name, "types": [t.upper() for t in b.types],
                                    "base_stats": b.base} for b in op_b]}
    tmpl = {"type": "select_lineup", "roster": [b.sid for b in my_b], "bring_count": 4, "lead_count": 2,
            "instructions": "Choose exactly 4..."}
    lineup = unwrap(agent.choose_action(gs("team_preview", preview, [{"action_id": "select_lineup", "label": "x",
                                                                      "input": {"action": tmpl}}], 0), CTX))
    bring = [next(b for b in my_b if b.sid == to_id(s)) for s in lineup["bring"]]
    leads = [next(i for i, b in enumerate(bring) if b.sid == to_id(s)) for s in lineup["leads"]]
    ob, ol = naive.preview(op_b, my_b, rng)
    their4 = [op_b[i] for i in ob]
    tl = [ob.index(ol[0]), ob.index(ol[1])]
    if verbose:
        print("bring:", [b.name for b in bring], "leads:", [bring[i].name for i in leads])

    s0 = Side([Mon.fresh(b) for b in bring], list(leads))
    s1 = Side([Mon.fresh(b) for b in their4], tl)
    for i, m in enumerate(s1.mons):
        m.revealed = i in tl
    st = State([s0, s1], Field())
    start_battle(st)
    version = [0]

    def replace(state, side, slot):
        if side == 1:
            c = replace_best(state, side, slot)
            if c is not None:
                state.sides[1].mons[c].revealed = True
            return c
        forced = [slot == 0, slot == 1]
        version[0] += 1
        t = doubles_template(state, 0, forced=forced)
        d = unwrap(agent.choose_action(gs("moving", battle_obs(state, 0), [
            {"action_id": "doubles_turn", "label": "x", "input": {"action": t}}], version[0]), CTX))
        acts = to_engine(state.sides[0], d)
        a = acts[slot]
        if a[0] != "switch":
            a = next((x for x in acts if x[0] == "switch"), ("switch", state.sides[0].bench()[0]))
        return a[1]

    for _ in range(40):
        version[0] += 1
        t = doubles_template(st, 0)
        d = unwrap(agent.choose_action(gs("moving", battle_obs(st, 0), [
            {"action_id": "doubles_turn", "label": "x", "input": {"action": t}}], version[0]), CTX))
        mine = to_engine(st.sides[0], d)
        # validate like the server would
        for n, a in enumerate(mine):
            opts = t["slots"][n]["options"]
            if a[0] == "move":
                o = next((o for o in opts if o.get("type") == "move" and to_id(o["move_id"]) == a[1]), None)
                assert o is not None, f"illegal move {a} in {opts}"
                assert (not o["targets"] and a[2] in (0, None)) or a[2] in o["targets"], f"bad target {a} {o['targets']}"
        theirs = policy_greedy(st, 1, rng)
        if verbose:
            print(f"T{st.turn} us {mine} them {theirs}")
        st = resolve(st, [mine, theirs], rng=rng, replace=replace)
        w = st.winner()
        if w is not None:
            return w
    return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    import agent.agent as agent_mod
    agent_mod.PREVIEW_BUDGET_S = 6.0
    rng = random.Random(args.seed)
    wins = [0, 0, 0]
    t0 = time.time()
    for m in range(args.matches):
        w = run_match(rng, verbose=args.v)
        wins[w if w >= 0 else 2] += 1
        print(f"match {m + 1}: {['WIN', 'LOSS', 'DRAW'][w if w >= 0 else 2]} ({time.time() - t0:.0f}s)", flush=True)
    print(f"agent {wins[0]} - {wins[1]} naive (draws {wins[2]})")


if __name__ == "__main__":
    sys.exit(main())
