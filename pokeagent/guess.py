"""A stand-in set for an opponent card we never saw (it was drafted before our first look at the pool).

Built only from Showdown's static data (Pokédex and learnsets, the same reference data as the rest of
the dex): the species' strongest same-type attacks plus coverage and Protect, with EVs in its better
attacking stat. Moves the opponent reveals in battle replace the guesses (see `with_revealed`).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import dex
from .dex import to_id

EXCLUDE = {"explosion", "selfdestruct", "mistyexplosion", "finalgambit", "memento", "healingwish",
           "lunardance", "lastresort", "belch", "dreameater", "focuspunch", "synchronoise", "steelbeam",
           "mindblown", "skydrop", "shellsmash", "futuresight", "doomdesire", "snore", "sleeptalk",
           "struggle", "naturalgift", "fling", "spitup", "counter", "mirrorcoat", "metalburst", "bide",
           "beatup", "present", "magnitude", "rollout", "iceball", "uproar", "thrash", "outrage", "petaldance",
           "ragingfury", "frustration", "return", "hiddenpower", "solarbeam", "solarblade", "meteorbeam",
           "electroshot", "skyattack", "razorwind", "freezeshock", "iceburn", "geomancy", "phantomforce",
           "shadowforce", "fly", "dig", "dive", "bounce", "hyperbeam", "gigaimpact", "blastburn", "hydrocannon",
           "frenzyplant", "rockwrecker", "roaroftime", "prismaticlaser", "eternabeam", "meteorassault"}


# General VGC knowledge of which species usually carry a support move (not tied to any card list)
USUAL_SUPPORT = {
    "fakeout": {"incineroar", "rillaboom", "ironhands", "sneasler", "sableye", "hariyama", "mienshao", "weavile",
                "ambipom", "scrafty", "kangaskhan", "meowscarada", "persianalola", "ludicolo", "lopunny", "mrrime",
                "toxicroak", "infernape", "cinccino", "raichu", "grimmsnarl"},
    "ragepowder": {"amoonguss", "volcarona"},
    "followme": {"indeedeef", "indeedee", "clefairy", "clefable", "maushold", "togekiss", "ogerpon",
                 "ogerponhearthflame", "ogerponwellspring", "ogerponcornerstone", "ursaring"},
    "spore": {"amoonguss", "brelooom", "breloom", "toedscruel", "smeargle"},
    "tailwind": {"whimsicott", "tornadus", "talonflame", "murkrow", "kilowattrel", "pelipper", "salamence",
                 "dragonite", "corviknight", "noivern", "zapdos", "suicune", "brambleghast"},
    "trickroom": {"farigiraf", "hatterene", "cresselia", "porygon2", "indeedeef", "dusclops", "oranguru",
                  "armarouge", "sinistcha", "bronzong", "slowbro", "reuniclus", "mimikyu"},
    "willowisp": {"sableye"},
    "lightscreen": {"grimmsnarl", "klefki"},
    "reflect": {"grimmsnarl", "klefki"},
}


@lru_cache(maxsize=1)
def _learnsets() -> dict:
    import poke_env
    path = Path(poke_env.__file__).parent / "data" / "static" / "learnset.json"
    return json.loads(path.read_text())


def learnable(species_id: str) -> set[str]:
    ls = _learnsets()
    entry = dex.species(species_id)
    out: set[str] = set()
    for key in (to_id(entry["name"]), to_id(entry.get("baseSpecies") or ""), to_id(entry.get("changesFrom") or "")):
        if key and key in ls:
            out |= set((ls[key].get("learnset") or {}).keys())
    return out


@lru_cache(maxsize=None)
def guess_card(species: str, ability: str = "") -> dict:
    entry = dex.species(species)
    base = entry["baseStats"]
    types = entry["types"]
    physical = base["atk"] >= base["spa"]
    cat = "Physical" if physical else "Special"
    moves = learnable(species)
    scored = []
    for mid in moves:
        if mid in EXCLUDE or mid not in dex.MOVES:
            continue
        md = dex.MOVES[mid]
        if md.get("category") != cat or md.get("isZ") or md.get("isMax"):
            continue
        fl = md.get("flags") or {}
        if fl.get("charge") or fl.get("recharge"):
            continue
        bp = md.get("basePower") or 0
        mh = md.get("multihit")
        hits = 3.1 if isinstance(mh, list) else float(mh or 1)
        power = bp * hits * (1.5 if md.get("willCrit") else 1.0)
        if power < 50:
            continue
        acc = 1.0 if md.get("accuracy") is True else (md.get("accuracy") or 100) / 100
        s = power * acc * (1.5 if md["type"] in types else 1.0)
        if md.get("target") in ("allAdjacentFoes",):
            s *= 1.1
        if md.get("self") and (md["self"].get("boosts") or {}).get("spa", 0) <= -2:
            s *= 0.85
        scored.append((s, mid, md["type"]))
    scored.sort(reverse=True)
    sid = to_id(entry["name"])
    support = [m for m, users in USUAL_SUPPORT.items() if sid in users and m in moves][:2]
    picked, types_used = list(support), set()
    attack_slots = 3 - len(support) if "protect" in moves else 4 - len(support)
    for s, mid, t in scored:  # best move of each type first, same-type ones naturally on top
        if t in types_used:
            continue
        if attack_slots <= 0:
            break
        picked.append(mid)
        types_used.add(t)
        attack_slots -= 1
    if "protect" in moves:
        picked.append("protect")
    for s, mid, t in scored:
        if len(picked) >= 4:
            break
        if mid not in picked:
            picked.append(mid)
    fast = base["spe"] >= 70
    main = "atk" if physical else "spa"
    evs = {"hp": 4, main: 252, "spe": 252} if fast else {"hp": 252, main: 252, "spd": 4}
    nature = ("Jolly" if physical else "Timid") if fast else ("Adamant" if physical else "Modest")
    abilities = list(entry.get("abilities", {}).values())
    return {"card_id": "guess:" + to_id(entry["name"]), "species": entry["name"], "item": "",
            "ability": ability or (abilities[0] if abilities else ""), "nature": nature, "level": 50,
            "evs": evs, "ivs": {}, "moves": picked[:4]}


def with_revealed(card: dict, revealed: list[str]) -> dict:
    """The guessed card with the moves seen in battle in place of guesses (at most four)."""
    real = [to_id(m) for m in revealed if to_id(m) in dex.MOVES]
    rest = [m for m in card["moves"] if m not in real]
    return {**card, "moves": (real + rest)[:4]}
