"""Optional LLM battle advisor: on close turns, Claude chooses among the search's top plans.

The search (pokeagent.search.decide) scores every joint plan; when its best few are close, Claude
sees the board and those plans and picks one, bringing strategy the 2-turn search can't see
(speed control timing, preserving a win condition, endgames). It never invents a plan, so every
choice stays legal. A per-battle time budget keeps us far inside Showdown's 420 s battle clock.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from . import llm_draft
from .calc import move_data
from .model import State

MODEL = "claude-opus-5-5"
EFFORT = "low"
TIMEOUT_S = 10.0
CLOSE = 30.0         # consult only when the runner-up is within this many points (a Pokémon is ~100)
MAX_PLANS = 3
BATTLE_BUDGET_S = 100.0
STATS: Counter = Counter()  # asked / answered / changed / failed / no_answer, for offline reports
# Consult only when the close plans differ in something strategic (setup, Protect, switching) or in
# the endgame: the decisions a 2-turn search judges worst
STRATEGIC = False
_SETUP = {"tailwind", "trickroom", "reflect", "lightscreen", "auroraveil", "protect", "detect", "spikyshield",
          "kingsshield", "silktrap", "banefulbunker", "burningbulwark", "partingshot", "uturn", "voltswitch"}
# Hard spending cap for offline tests: tokens priced high on purpose (USD per million), and Claude is
# no longer asked once this process has spent the cap
SPEND_CAP_USD = float(os.environ.get("LLM_SPEND_CAP_USD", "inf"))
PRICE_IN, PRICE_OUT = 15.0, 75.0
SPENT_USD = 0.0


def _strategic_kinds(plan) -> frozenset:
    return frozenset(a[1] if a[0] == "move" and a[1] in _SETUP else "switch" if a[0] == "switch" else "-"
                     for a in plan)


def is_strategic(st: State, plans) -> bool:
    if any(_strategic_kinds(p) != _strategic_kinds(plans[0][0]) for p, _ in plans[1:]):
        return True
    left = [sum(1 for m in sd.mons if m.alive and not m.vol.get("not_brought")) + sd.unseen for sd in st.sides]
    return max(left) <= 2


def _mon_line(st: State, side: int, i: int, show_moves: bool) -> str:
    m = st.sides[side].mons[i]
    bits = [f"{m.name} {round(100 * m.hp / max(1, m.maxhp))}% HP"]
    if m.status:
        bits.append(m.status)
    boosts = ", ".join(f"{k} {v:+d}" for k, v in m.boosts.items() if v)
    if boosts:
        bits.append(boosts)
    if m.item:
        bits.append(f"item {m.item}")
    bits.append(f"ability {m.ability or m.build.ability}")
    bits.append(f"Spe {m.build.stats['spe']}")
    if show_moves:
        bits.append("moves " + ", ".join(m.build.moves))
    return "; ".join(bits)


def describe_board(st: State) -> str:
    out = [f"Turn {st.turn}."]
    f = st.field
    fx = [x for x in (f"weather {f.weather} ({f.weather_turns} turns)" if f.weather else "",
                      f"terrain {f.terrain} ({f.terrain_turns} turns)" if f.terrain else "",
                      f"Trick Room ({f.trickroom} turns)" if f.trickroom else "") if x]
    out.append("Field: " + (", ".join(fx) if fx else "nothing active") + ".")
    for side, who in ((0, "Our"), (1, "Opponent's")):
        sd = st.sides[side]
        conds = [x for x in (f"Tailwind {sd.tailwind}" if sd.tailwind else "", f"Reflect {sd.reflect}" if sd.reflect else "",
                             f"Light Screen {sd.lightscreen}" if sd.lightscreen else "",
                             f"Aurora Veil {sd.auroraveil}" if sd.auroraveil else "") if x]
        out.append(f"{who} side" + (f" ({', '.join(conds)})" if conds else "") + ":")
        for slot in (0, 1):
            i = sd.active[slot]
            if i is not None and sd.mons[i].alive:
                out.append(f"  active {'AB'[slot]}: " + _mon_line(st, side, i, True))
        bench = [i for i, m in enumerate(sd.mons) if i not in sd.active and m.alive and not m.vol.get("not_brought")]
        for i in bench:
            out.append("  bench: " + _mon_line(st, side, i, True))  # every set is public in the draft
        fainted = sum(1 for m in sd.mons if m.fainted)
        out.append(f"  fainted: {fainted}" + (f"; not yet seen: {sd.unseen}" if sd.unseen else ""))
    return "\n".join(out)


def describe_plan(st: State, side: int, plan) -> str:
    parts = []
    for slot, act in enumerate(plan):
        mon = st.sides[side].active_mon(slot)
        if mon is None or act[0] == "pass":
            continue
        if act[0] == "switch":
            parts.append(f"{mon.name} switches to {st.sides[side].mons[act[1]].name}")
            continue
        name = move_data(act[1]).get("name", act[1])
        tgt = ""
        if act[2] in (1, 2):
            t = st.sides[1 - side].active_mon(act[2] - 1)
            tgt = f" into {t.name}" if t else ""
        elif act[2] in (-1, -2) and -act[2] - 1 != slot:
            t = st.sides[side].active_mon(-act[2] - 1)
            tgt = f" on {t.name}" if t else ""
        parts.append(f"{mon.name} uses {name}{tgt}")
    return "; ".join(parts) or "pass"


def _prompt(st: State, plans, likely) -> str:
    opts = "\n".join(f"{n + 1}. {describe_plan(st, 0, p)} (search value {v:+.0f})" for n, (p, v) in enumerate(plans))
    opp = "\n".join(f"- {describe_plan(st, 1, p)} ({100 * q:.0f}%)" for p, q in likely[:3])
    return f"""You are playing a Pokémon VGC doubles battle (Gen 9, Regulation I, level 50, no Tera). \
Each side brought 4 Pokémon; the side that faints all of the other's wins.

{describe_board(st)}

Our 2-turn search rates these plans for this turn nearly equal (a Pokémon is worth about 100 points):
{opts}

It expects the opponent to do one of:
{opp or '- (unknown)'}

Pick the plan that gives us the best chance to win the whole game, not just this turn: think about \
speed order, who threatens a knockout, Protect, switching, preserving our win condition, and the \
endgame. Answer with only the plan number in square brackets, e.g. [1]."""


class BattleAdvisor:
    """One per battle: tracks the time Claude has used so the battle clock is never at risk."""

    def __init__(self):
        self.spent = 0.0

    def choose(self, st: State, ranked, likely):
        """A plan from `ranked` chosen by Claude, or None to keep the search's best."""
        global SPENT_USD
        import time
        plans = [(p, v) for p, v in ranked[:MAX_PLANS] if ranked[0][1] - v <= CLOSE]
        if len(plans) < 2 or self.spent > BATTLE_BUDGET_S - TIMEOUT_S or SPENT_USD >= SPEND_CAP_USD:
            return None
        if STRATEGIC and not is_strategic(st, plans):
            return None
        client = llm_draft._get_client()
        if client is None:
            return None
        STATS["asked"] += 1
        t0 = time.time()
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=3000, timeout=TIMEOUT_S, extra_body={"output_config": {"effort": EFFORT}},
                messages=[{"role": "user", "content": _prompt(st, plans, likely)}])
            text = "".join(getattr(b, "text", "") for b in msg.content)
            SPENT_USD += (msg.usage.input_tokens * PRICE_IN + msg.usage.output_tokens * PRICE_OUT) / 1e6
        except Exception:
            STATS["failed"] += 1
            return None
        finally:
            self.spent += time.time() - t0
        for found in re.findall(r"\[(\d+)\]", text) + re.findall(r"^\s*(\d+)\s*$", text):
            k = int(found) - 1
            if 0 <= k < len(plans):
                STATS["answered"] += 1
                STATS["changed"] += k != 0
                return plans[k][0]
        STATS["no_answer"] += 1
        return None
