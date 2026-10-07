"""Cross-check pokeagent.calc against poke-env's port of the Smogon calc on random matchups."""
import logging, random, sys
from poke_env.battle import DoubleBattle, Pokemon, Move
from poke_env.teambuilder import TeambuilderPokemon
from poke_env.calc.damage_calc_gen9 import calculate_damage
from pokeagent.model import Build, Mon, Side, Field, State
from pokeagent.calc import damage
from pokeagent.sample_cards import SAMPLE_CARDS

def tb(card):
    b = Build.from_card(card)
    evs = [b.evs[s] for s in ("hp","atk","def","spa","spd","spe")]
    ivs = [b.ivs[s] for s in ("hp","atk","def","spa","spd","spe")]
    return TeambuilderPokemon(species=card["species"], item=card["item"], ability=card["ability"], moves=card["moves"],
                              nature=card["nature"], evs=evs, ivs=ivs, level=50)

random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
bad = total = 0
for trial in range(400):
    a, d = random.sample(SAMPLE_CARDS, 2)
    battle = DoubleBattle("tag", "me", logging.getLogger("x"), gen=9)
    battle._player_role, battle._opponent_role = "p1", "p2"
    pa = Pokemon(gen=9, teambuilder=tb(a)); pd = Pokemon(gen=9, teambuilder=tb(d))
    battle._team = {f"p1: {pa.species}": pa}; battle._opponent_team = {f"p2: {pd.species}": pd}
    ba, bd = Build.from_card(a), Build.from_card(d)
    ma, md = Mon.fresh(ba), Mon.fresh(bd)
    st = State([Side([ma], [0, None]), Side([md], [0, None])], Field())
    for mv in ba.moves:
        try:
            lo, hi = calculate_damage(f"p1: {pa.species}", f"p2: {pd.species}", Move(mv, gen=9), battle)
        except Exception as e:
            continue
        r = damage(st, 0, ma, 1, md, mv)
        total += 1
        if (r[0], r[-1]) != (lo, hi):
            bad += 1
            print(f"{ba.name} {mv} -> {bd.name}: mine {r[0]}-{r[-1]}  poke-env {lo}-{hi}")
print(f"{total - bad}/{total} match")
