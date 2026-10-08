"""Head-to-head on real cards: two battle policies, same teams from both sides.

    PYTHONPATH=. .venv/bin/python tools/compare_battle_policies.py search2 search 60
"""
import json, random, sys, time
from pokeagent import sim
from pokeagent.model import Build

a, b, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 0
cards = json.load(open("tools/real_cards.json"))
rng = random.Random(seed)
wins = [0, 0, 0]
t0 = time.time()
for g in range(n // 2):
    picked, items = [], set()
    for c in rng.sample(cards, len(cards)):
        if c["item"] not in items:
            picked.append(c); items.add(c["item"])
        if len(picked) == 8:
            break
    t1, t2 = [Build.from_card(c) for c in picked[:4]], [Build.from_card(c) for c in picked[4:]]
    for swap in (False, True):
        x, y = (t2, t1) if swap else (t1, t2)
        w = sim.play_sides(x, y, sim.POLICIES[a], sim.POLICIES[b], rng)
        wins[w if w >= 0 else 2] += 1
    print(f"{2 * (g + 1)} games: {a} {wins[0]} - {wins[1]} {b} (draws {wins[2]}) {time.time() - t0:.0f}s", flush=True)
