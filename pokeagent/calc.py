"""Gen 9 doubles damage calculator, following Smogon's calc (mechanics/gen789.ts) step by step.

`damage()` returns the 16 possible totals (all hits of a multi-hit move summed). It covers
what matters at VGC level 50: STAB, types, spread, weather, terrain, crits, burn, boosts,
screens, Ruin abilities, Protosynthesis/Quark Drive, the common items and abilities, and the
variable-power moves. Things it ignores are rare in this format (Tera, Z/Dynamax, Ally Switch).
"""

from __future__ import annotations

import math
from functools import lru_cache

from . import dex
from .model import Mon, State

BOOST_TABLE = {-6: 2 / 8, -5: 2 / 7, -4: 2 / 6, -3: 2 / 5, -2: 2 / 4, -1: 2 / 3, 0: 1,
               1: 3 / 2, 2: 4 / 2, 3: 5 / 2, 4: 6 / 2, 5: 7 / 2, 6: 8 / 2}

TYPE_ITEMS = {
    "Normal": "silkscarf", "Fire": "charcoal", "Water": "mysticwater", "Electric": "magnet",
    "Grass": "miracleseed", "Ice": "nevermeltice", "Fighting": "blackbelt", "Poison": "poisonbarb",
    "Ground": "softsand", "Flying": "sharpbeak", "Psychic": "twistedspoon", "Bug": "silverpowder",
    "Rock": "hardstone", "Ghost": "spelltag", "Dragon": "dragonfang", "Dark": "blackglasses",
    "Steel": "metalcoat", "Fairy": "fairyfeather",
}
PLATE_ITEMS = {
    "Fire": "flameplate", "Water": "splashplate", "Electric": "zapplate", "Grass": "meadowplate",
    "Ice": "icicleplate", "Fighting": "fistplate", "Poison": "toxicplate", "Ground": "earthplate",
    "Flying": "skyplate", "Psychic": "mindplate", "Bug": "insectplate", "Rock": "stoneplate",
    "Ghost": "spookyplate", "Dragon": "dracoplate", "Dark": "dreadplate", "Steel": "ironplate",
    "Fairy": "pixieplate",
}
RESIST_BERRIES = {
    "occaberry": "Fire", "passhoberry": "Water", "wacanberry": "Electric", "rindoberry": "Grass",
    "yacheberry": "Ice", "chopleberry": "Fighting", "kebiaberry": "Poison", "shucaberry": "Ground",
    "cobaberry": "Flying", "payapaberry": "Psychic", "tangaberry": "Bug", "chartiberry": "Rock",
    "kasibberry": "Ghost", "habanberry": "Dragon", "colburberry": "Dark", "babiriberry": "Steel",
    "roseliberry": "Fairy", "chilanberry": "Normal",
}
ABILITY_IMMUNE = {
    "levitate": "Ground", "flashfire": "Fire", "wellbakedbody": "Fire", "waterabsorb": "Water",
    "stormdrain": "Water", "dryskin": "Water", "voltabsorb": "Electric", "lightningrod": "Electric",
    "motordrive": "Electric", "sapsipper": "Grass", "eartheater": "Ground",
}
MOLD_BREAKERS = {"moldbreaker", "teravolt", "turboblaze", "myceliummight"}
SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}
NO_ITEM_LOSS = {"", "griseouscore", "rustedsword", "rustedshield"}
FIXED_LEVEL_MOVES = {"seismictoss", "nightshade"}
HALF_HP_MOVES = {"superfang", "ruination", "naturesmadness"}
OGERPON_MASKS = {"wellspringmask": "Water", "hearthflamemask": "Fire", "cornerstonemask": "Rock"}

Rolls = tuple[int, ...]
ZERO: Rolls = (0,) * 16


def poke_round(x: float) -> int:
    return math.ceil(x - 0.5) if x % 1 > 0.5 else math.floor(x)


def chain(mods: list[int]) -> int:
    m = 4096
    for mod in mods:
        if mod != 4096:
            m = (m * mod + 2048) >> 12
    return m


def apply_mod(value: int, mod: int) -> int:
    return poke_round(value * mod / 4096)


@lru_cache(maxsize=None)
def move_data(move_id: str) -> dict:
    return dex.move(move_id)


def flags(move_id: str) -> dict:
    return move_data(move_id).get("flags") or {}


def is_spread(move_id: str) -> bool:
    return move_data(move_id).get("target") in SPREAD_TARGETS


def _ally(state: State, side: int, mon: Mon) -> Mon | None:
    for slot in (0, 1):
        other = state.sides[side].active_mon(slot)
        if other is not None and other is not mon:
            return other
    return None


def _field_has_ability(state: State, ability: str, exclude: Mon) -> bool:
    return any(m.ability == ability and m is not exclude for _, _, m in state.actives())


def _proto_stat(mon: Mon, state: State) -> str | None:
    """Which stat Protosynthesis / Quark Drive boosts right now, or None."""
    if mon.ability not in ("protosynthesis", "quarkdrive"):
        return None
    active = mon.vol.get("proto")
    if not active:
        if mon.ability == "protosynthesis" and state.field.weather == "sun":
            active = True
        elif mon.ability == "quarkdrive" and state.field.terrain == "electric":
            active = True
    if not active:
        return None
    best, best_val = None, -1
    for s in ("atk", "def", "spa", "spd", "spe"):
        v = math.floor(mon.stat(s) * BOOST_TABLE[mon.boosts[s]])
        if v > best_val:
            best, best_val = s, v
    return best


def effective_speed(mon: Mon, state: State, side: int) -> int:
    spe = math.floor(mon.stat("spe") * BOOST_TABLE[mon.boosts["spe"]])
    mods = []
    if state.sides[side].tailwind:
        mods.append(8192)
    w = state.field.weather
    if (mon.ability == "chlorophyll" and w == "sun") or (mon.ability == "swiftswim" and w == "rain") \
            or (mon.ability == "sandrush" and w == "sand") or (mon.ability == "slushrush" and w == "snow") \
            or (mon.ability == "surgesurfer" and state.field.terrain == "electric"):
        mods.append(8192)
    if mon.ability == "unburden" and mon.vol.get("unburden"):
        mods.append(8192)
    if mon.item == "choicescarf":
        mods.append(6144)
    if _proto_stat(mon, state) == "spe":
        mods.append(6144)
    if mon.item in ("ironball", "machobrace", "powerweight", "powerbracer", "powerbelt", "powerlens",
                    "powerband", "poweranklet"):
        mods.append(2048)
    spe = apply_mod(spe, chain(mods))
    if mon.status == "par" and mon.ability != "quickfeet":
        spe = math.floor(spe * 0.5)
    return min(spe, 10000)


def move_type(attacker: Mon, move_id: str, state: State) -> str:
    md = move_data(move_id)
    t = md["type"]
    w, terr = state.field.weather, state.field.terrain
    if move_id == "weatherball":
        t = {"sun": "Fire", "rain": "Water", "sand": "Rock", "snow": "Ice"}.get(w, "Normal")
    elif move_id == "terrainpulse" and attacker.grounded(state.field):
        t = {"electric": "Electric", "grassy": "Grass", "psychic": "Psychic", "misty": "Fairy"}.get(terr, "Normal")
    elif move_id == "ivycudgel":
        t = OGERPON_MASKS.get(attacker.item, "Grass")
    elif move_id == "ragingbull":
        t = attacker.types[-1] if attacker.sid.startswith("taurospaldea") else "Normal"
    elif move_id in ("judgment", "multiattack"):
        t = next((k for k, v in PLATE_ITEMS.items() if v == attacker.item), t)
    elif move_id == "aurawheel" and attacker.sid == "morpekohangry":
        t = "Dark"
    if t == "Normal" and md["category"] != "Status":
        ate = {"pixilate": "Fairy", "aerilate": "Flying", "refrigerate": "Ice", "galvanize": "Electric"}
        if attacker.ability in ate:
            t = ate[attacker.ability]
    if attacker.ability == "normalize":
        t = "Normal"
    if attacker.ability == "liquidvoice" and flags(move_id).get("sound"):
        t = "Water"
    return t


def effectiveness(attacker: Mon, defender: Mon, move_id: str, mtype: str, state: State,
                  ignore_ability: bool = False) -> float:
    """Type multiplier including immunities from type, ability and item (0 = no effect)."""
    def_types = defender.types
    if attacker.ability in ("scrappy", "mindseye") and mtype in ("Normal", "Fighting"):
        def_types = tuple(t for t in def_types if t != "Ghost") or ("Normal",)
    eff = dex.type_eff(mtype, def_types)
    if move_id == "freezedry" and "Water" in defender.types:
        eff *= 4.0  # Water's 0.5 becomes 2
    if move_id == "flyingpress":
        eff *= dex.type_eff("Flying", def_types)
    if move_id == "thousandarrows" and "Flying" in defender.types and eff == 0:
        eff = dex.type_eff("Ground", tuple(t for t in def_types if t != "Flying") or ("Normal",))
    if mtype == "Ground" and eff > 0 and not defender.grounded(state.field) and move_id != "thousandarrows":
        eff = 0.0
    if mtype == "Ground" and defender.item == "ironball" and "Flying" in defender.types:
        eff = dex.type_eff("Ground", tuple(t for t in def_types if t != "Flying") or ("Normal",))
    ability = "" if ignore_ability else defender.ability
    if ability and ABILITY_IMMUNE.get(ability) == mtype:
        eff = 0.0
    if ability == "wonderguard" and eff <= 1:
        eff = 0.0
    if ability == "bulletproof" and flags(move_id).get("bullet"):
        eff = 0.0
    if ability == "soundproof" and flags(move_id).get("sound"):
        eff = 0.0
    if ability == "windrider" and flags(move_id).get("wind"):
        eff = 0.0
    return eff


def defender_ability_ignored(attacker: Mon, move_id: str, defender: Mon) -> bool:
    if defender.item == "abilityshield":
        return False
    if attacker.ability in MOLD_BREAKERS:
        return True
    return move_id in ("sunsteelstrike", "moongeistbeam", "photongeyser", "menacingmoonrazemaelstrom")


def num_hits(attacker: Mon, move_id: str) -> float:
    md = move_data(move_id)
    mh = md.get("multihit")
    if move_id == "populationbomb":
        return 10 if attacker.item == "widelens" or attacker.ability == "skilllink" else 6.0
    if move_id == "tripleaxel":
        return 1.0  # handled as a power multiplier instead
    if mh is None:
        return 2.0 if attacker.ability == "parentalbond" and md["category"] != "Status" else 1.0
    if isinstance(mh, list):
        lo, hi = mh
        if attacker.ability == "skilllink":
            return float(hi)
        if attacker.item == "loadeddice":
            return 4.5 if hi == 5 else float(hi)
        return 3.1 if (lo, hi) == (2, 5) else (lo + hi) / 2
    return float(mh)


def base_power(attacker: Mon, defender: Mon, move_id: str, state: State, a_side: int, mtype: str,
               moved_first: bool | None) -> int:
    md = move_data(move_id)
    bp = md.get("basePower") or 0
    hp_ratio = attacker.hp / attacker.maxhp if attacker.maxhp else 1
    if move_id in ("eruption", "waterspout", "dragonenergy"):
        bp = max(1, math.floor(150 * attacker.hp / attacker.maxhp))
    elif move_id in ("flail", "reversal"):
        p = math.floor(48 * attacker.hp / attacker.maxhp)
        bp = 200 if p <= 1 else 150 if p <= 4 else 100 if p <= 9 else 80 if p <= 16 else 40 if p <= 32 else 20
    elif move_id in ("lowkick", "grassknot"):
        w = defender.build.weight
        bp = 120 if w >= 200 else 100 if w >= 100 else 80 if w >= 50 else 60 if w >= 25 else 40 if w >= 10 else 20
    elif move_id in ("heavyslam", "heatcrash"):
        r = attacker.build.weight / max(defender.build.weight, 0.1)
        bp = 120 if r >= 5 else 100 if r >= 4 else 80 if r >= 3 else 60 if r >= 2 else 40
    elif move_id in ("storedpower", "powertrip"):
        bp = 20 + 20 * sum(max(0, v) for k, v in attacker.boosts.items())
    elif move_id == "lastrespects":
        bp = min(5050, 50 + 50 * state.sides[a_side].fainted_count)
    elif move_id == "ragefist":
        bp = min(350, 50 + 50 * attacker.hits_taken)
    elif move_id == "hardpress":
        bp = max(1, math.floor(100 * defender.hp / defender.maxhp))
    elif move_id in ("crushgrip", "wringout"):
        bp = max(1, math.floor(120 * defender.hp / defender.maxhp))
    elif move_id == "tripleaxel":
        bp = 20 + 40 + 60  # total of the three hits; accuracy handled by the engine
    elif move_id in ("gyroball",):
        a = max(1, effective_speed(attacker, state, a_side))
        bp = min(150, math.floor(25 * effective_speed(defender, state, 1 - a_side) / a) + 1)
    elif move_id == "electroball":
        r = effective_speed(attacker, state, a_side) / max(1, effective_speed(defender, state, 1 - a_side))
        bp = 150 if r >= 4 else 120 if r >= 3 else 80 if r >= 2 else 60 if r >= 1 else 40
    elif move_id in ("facade",) and attacker.status in ("brn", "par", "psn", "tox"):
        bp *= 2
    elif move_id in ("hex", "infernalparade", "bittermalice") and defender.status:
        bp *= 2
    elif move_id == "brine" and defender.hp * 2 <= defender.maxhp:
        bp *= 2
    elif move_id in ("venoshock", "barbbarrage") and defender.status in ("psn", "tox"):
        bp *= 2
    elif move_id == "acrobatics" and not attacker.item:
        bp *= 2
    elif move_id in ("boltbeak", "fishiousrend") and moved_first:
        bp *= 2
    elif move_id in ("payback",) and moved_first is False:
        bp *= 2
    elif move_id == "weatherball" and state.field.weather:
        bp *= 2
    elif move_id == "terrainpulse" and state.field.terrain and attacker.grounded(state.field):
        bp *= 2
    elif move_id == "expandingforce" and state.field.terrain == "psychic" and attacker.grounded(state.field):
        bp = math.floor(bp * 1.5)
    elif move_id == "risingvoltage" and state.field.terrain == "electric" and defender.grounded(state.field):
        bp *= 2
    elif move_id == "mistyexplosion" and state.field.terrain == "misty" and attacker.grounded(state.field):
        bp = math.floor(bp * 1.5)
    elif move_id in ("solarbeam", "solarblade") and state.field.weather in ("rain", "sand", "snow"):
        bp = bp // 2
    elif move_id == "temperflare" and attacker.vol.get("last_failed"):
        bp *= 2
    if move_id == "knockoff" and defender.item not in NO_ITEM_LOSS and not (
            defender.sid.startswith("ogerpon") and defender.item.endswith("mask")):
        pass  # 1.5x applied in the modifier chain below
    return bp


def damage(state: State, a_side: int, attacker: Mon, d_side: int, defender: Mon, move_id: str, *,
           spread: bool = False, crit: bool = False, helping: bool = False,
           moved_first: bool | None = None) -> Rolls:
    """All 16 damage totals for `attacker` using `move_id` on `defender` (0s if it can't damage)."""
    md = move_data(move_id)
    category = md["category"]
    if category == "Status" or not attacker.alive or not defender.alive:
        return ZERO
    if move_id in ("photongeyser", "terablast") or (move_id == "shellsidearm"):
        atk = attacker.stat("atk") * BOOST_TABLE[attacker.boosts["atk"]]
        spa = attacker.stat("spa") * BOOST_TABLE[attacker.boosts["spa"]]
        if move_id == "shellsidearm":
            category = "Physical" if atk / defender.stat("def") > spa / defender.stat("spd") else "Special"
        elif atk > spa:
            category = "Physical"
    fl = flags(move_id)
    ignore_def_ability = defender_ability_ignored(attacker, move_id, defender)
    d_ability = "" if ignore_def_ability else defender.ability
    mtype = move_type(attacker, move_id, state)
    eff = effectiveness(attacker, defender, move_id, mtype, state, ignore_ability=ignore_def_ability)
    if eff == 0:
        return ZERO
    if mtype == "Ground" and defender.item == "airballoon":
        return ZERO
    if d_ability in ("armortail", "queenlymajesty", "dazzling") and md.get("priority", 0) > 0 and d_side != a_side:
        return ZERO
    if state.field.terrain == "psychic" and md.get("priority", 0) > 0 and defender.grounded(state.field) \
            and d_side != a_side:
        return ZERO
    if d_ability == "goodasgold" and category == "Status":
        return ZERO
    if d_ability == "telepathy" and d_side == a_side:
        return ZERO

    # Fixed damage
    if move_id in FIXED_LEVEL_MOVES:
        return (attacker.build.level,) * 16
    if move_id in HALF_HP_MOVES:
        return (max(1, defender.hp // 2),) * 16
    if move_id == "finalgambit":
        return (attacker.hp,) * 16
    if move_id == "endeavor":
        return (max(0, defender.hp - attacker.hp),) * 16

    if md.get("willCrit") or (attacker.ability == "merciless" and defender.status in ("psn", "tox")):
        crit = True
    if d_ability in ("battlearmor", "shellarmor"):
        crit = False

    ally = _ally(state, a_side, attacker)
    w = state.field.weather
    if any(m.ability in ("cloudnine", "airlock") for _, _, m in state.actives()):
        w = ""
    terr = state.field.terrain

    # ---- Base power ----
    bp = base_power(attacker, defender, move_id, state, a_side, mtype, moved_first)
    if bp <= 0:
        return ZERO
    bp_mods: list[int] = []
    if attacker.ability == "technician" and bp <= 60:
        bp_mods.append(6144)
    if attacker.ability == "toughclaws" and fl.get("contact") and attacker.item != "punchingglove":
        bp_mods.append(5325)
    if attacker.ability == "ironfist" and fl.get("punch"):
        bp_mods.append(4915)
    if attacker.ability == "strongjaw" and fl.get("bite"):
        bp_mods.append(6144)
    if attacker.ability == "megalauncher" and fl.get("pulse"):
        bp_mods.append(6144)
    if attacker.ability == "sharpness" and fl.get("slicing"):
        bp_mods.append(6144)
    if attacker.ability == "reckless" and (md.get("recoil") or md.get("hasCrashDamage")):
        bp_mods.append(4915)
    if attacker.ability == "sheerforce" and (md.get("secondary") or md.get("secondaries")):
        bp_mods.append(5325)
    if attacker.ability == "punkrock" and fl.get("sound"):
        bp_mods.append(5325)
    if attacker.ability == "sandforce" and w == "sand" and mtype in ("Rock", "Ground", "Steel"):
        bp_mods.append(5325)
    if attacker.ability == "analytic" and moved_first is False:
        bp_mods.append(5325)
    if attacker.ability == "supremeoverlord":
        n = min(5, state.sides[a_side].fainted_count)
        if n:
            bp_mods.append(4096 + 410 * n)
    if attacker.ability in ("aerilate", "pixilate", "refrigerate", "galvanize") and move_data(move_id)["type"] == "Normal":
        bp_mods.append(4915)
    if attacker.ability == "rockypayload" and mtype == "Rock":
        bp_mods.append(6144)
    if attacker.ability in ("steelworker", "steelyspirit") and mtype == "Steel":
        bp_mods.append(6144)
    if ally is not None:
        if ally.ability == "steelyspirit" and mtype == "Steel":
            bp_mods.append(6144)
        if ally.ability == "battery" and category == "Special":
            bp_mods.append(5325)
        if ally.ability == "powerspot":
            bp_mods.append(5325)
    if any(m.ability == "fairyaura" for _, _, m in state.actives()) and mtype == "Fairy":
        bp_mods.append(5448)
    if any(m.ability == "darkaura" for _, _, m in state.actives()) and mtype == "Dark":
        bp_mods.append(5448)
    if helping:
        bp_mods.append(6144)
    if attacker.vol.get("charge") and mtype == "Electric":
        bp_mods.append(8192)
    if move_id == "knockoff" and defender.item not in NO_ITEM_LOSS and not (
            defender.sid.startswith("ogerpon") and defender.item.endswith("mask")):
        bp_mods.append(6144)
    if move_id == "psyblade" and terr == "electric":
        bp_mods.append(6144)
    if move_id in ("collisioncourse", "electrodrift") and eff > 1:
        bp_mods.append(5461)
    if d_ability == "heatproof" and mtype == "Fire":
        bp_mods.append(2048)
    if d_ability == "dryskin" and mtype == "Fire":
        bp_mods.append(5120)
    # items
    if attacker.item and TYPE_ITEMS.get(mtype) == attacker.item or PLATE_ITEMS.get(mtype) == attacker.item:
        bp_mods.append(4915)
    if attacker.item in OGERPON_MASKS and attacker.sid.startswith("ogerpon"):
        bp_mods.append(4915)
    if attacker.item == "muscleband" and category == "Physical":
        bp_mods.append(4505)
    if attacker.item == "wiseglasses" and category == "Special":
        bp_mods.append(4505)
    if attacker.item == "punchingglove" and fl.get("punch"):
        bp_mods.append(4506)
    if attacker.item in ("adamantorb", "adamantcrystal") and attacker.sid.startswith("dialga") and mtype in ("Steel", "Dragon"):
        bp_mods.append(4915)
    if attacker.item in ("lustrousorb", "lustrousglobe") and attacker.sid.startswith("palkia") and mtype in ("Water", "Dragon"):
        bp_mods.append(4915)
    if attacker.item in ("griseousorb", "griseouscore") and attacker.sid.startswith("giratina") and mtype in ("Ghost", "Dragon"):
        bp_mods.append(4915)
    # terrain
    if attacker.grounded(state.field):
        if (terr == "electric" and mtype == "Electric") or (terr == "grassy" and mtype == "Grass") \
                or (terr == "psychic" and mtype == "Psychic"):
            bp_mods.append(5325)
    if defender.grounded(state.field):
        if terr == "misty" and mtype == "Dragon":
            bp_mods.append(2048)
        if terr == "grassy" and move_id in ("earthquake", "bulldoze", "magnitude"):
            bp_mods.append(2048)
    bp = max(1, apply_mod(bp, chain(bp_mods)))

    # ---- Attack ----
    if move_id == "bodypress":
        atk_stat, atk_mon = "def", attacker
    elif move_id == "foulplay":
        atk_stat, atk_mon = "atk", defender
    else:
        atk_stat, atk_mon = ("atk" if category == "Physical" else "spa"), attacker
    raw_atk = atk_mon.stat(atk_stat)
    a_boost = atk_mon.boosts[atk_stat]
    if d_ability == "unaware":
        a_boost = 0
    if crit and a_boost < 0:
        a_boost = 0
    attack = math.floor(raw_atk * BOOST_TABLE[a_boost])
    if attacker.ability == "hustle" and category == "Physical":
        attack = math.floor(attack * 1.5)
    at_mods: list[int] = []
    if attacker.ability in ("hugepower", "purepower") and category == "Physical":
        at_mods.append(8192)
    if attacker.item == "choiceband" and category == "Physical":
        at_mods.append(6144)
    if attacker.item == "choicespecs" and category == "Special":
        at_mods.append(6144)
    if attacker.ability == "gorillatactics" and category == "Physical":
        at_mods.append(6144)
    if attacker.ability == "guts" and attacker.status and category == "Physical":
        at_mods.append(6144)
    if attacker.ability == "solarpower" and w == "sun" and category == "Special":
        at_mods.append(6144)
    if attacker.ability == "flashfire" and attacker.vol.get("flashfire") and mtype == "Fire":
        at_mods.append(6144)
    if attacker.ability in ("blaze", "torrent", "overgrow", "swarm") and attacker.hp * 3 <= attacker.maxhp:
        if mtype == {"blaze": "Fire", "torrent": "Water", "overgrow": "Grass", "swarm": "Bug"}[attacker.ability]:
            at_mods.append(6144)
    if attacker.ability == "defeatist" and attacker.hp * 2 <= attacker.maxhp:
        at_mods.append(2048)
    if attacker.ability == "transistor" and mtype == "Electric":
        at_mods.append(5325)
    if attacker.ability == "dragonsmaw" and mtype == "Dragon":
        at_mods.append(6144)
    if attacker.ability == "waterbubble" and mtype == "Water":
        at_mods.append(8192)
    if attacker.ability == "orichalcumpulse" and w == "sun" and category == "Physical":
        at_mods.append(5461)
    if attacker.ability == "hadronengine" and terr == "electric" and category == "Special":
        at_mods.append(5461)
    proto = _proto_stat(attacker, state)
    if proto and proto == atk_stat and atk_mon is attacker:
        at_mods.append(5325)
    if d_ability in ("thickfat",) and mtype in ("Fire", "Ice"):
        at_mods.append(2048)
    if d_ability == "waterbubble" and mtype == "Fire":
        at_mods.append(2048)
    if d_ability == "purifyingsalt" and mtype == "Ghost":
        at_mods.append(2048)
    if category == "Physical" and _field_has_ability(state, "tabletsofruin", attacker):
        at_mods.append(3072)
    if category == "Special" and _field_has_ability(state, "vesselofruin", attacker):
        at_mods.append(3072)
    attack = max(1, apply_mod(attack, chain(at_mods)))

    # ---- Defense ----
    hits_physical_def = category == "Physical" or move_id in ("psyshock", "psystrike", "secretsword")
    def_stat = "def" if hits_physical_def else "spd"
    d_boost = defender.boosts[def_stat]
    if attacker.ability == "unaware" or move_id in ("sacredsword", "darkestlariat", "chipaway"):
        d_boost = 0
    if crit and d_boost > 0:
        d_boost = 0
    defense = math.floor(defender.stat(def_stat) * BOOST_TABLE[d_boost])
    if w == "sand" and defender.has_type("Rock") and def_stat == "spd":
        defense = math.floor(defense * 1.5)
    if w == "snow" and defender.has_type("Ice") and def_stat == "def":
        defense = math.floor(defense * 1.5)
    df_mods: list[int] = []
    if d_ability == "furcoat" and def_stat == "def":
        df_mods.append(8192)
    if d_ability == "marvelscale" and defender.status and def_stat == "def":
        df_mods.append(6144)
    if d_ability == "grasspelt" and terr == "grassy" and def_stat == "def":
        df_mods.append(6144)
    if defender.item == "eviolite" and dex.species(defender.name).get("evos"):
        df_mods.append(6144)
    if defender.item == "assaultvest" and def_stat == "spd":
        df_mods.append(6144)
    dproto = _proto_stat(defender, state)
    if dproto and dproto == def_stat:
        df_mods.append(5325)
    if def_stat == "def" and _field_has_ability(state, "swordofruin", defender):
        df_mods.append(3072)
    if def_stat == "spd" and _field_has_ability(state, "beadsofruin", defender):
        df_mods.append(3072)
    defense = max(1, apply_mod(defense, chain(df_mods)))

    # ---- Base damage ----
    level = attacker.build.level
    base = math.floor(math.floor(math.floor(2 * level / 5 + 2) * bp * attack / defense) / 50 + 2)
    if spread:
        base = apply_mod(base, 3072)
    if w == "sun" and mtype == "Fire" or w == "rain" and mtype == "Water":
        base = apply_mod(base, 6144)
    elif (w == "sun" and mtype == "Water" and move_id != "hydrosteam") or (w == "rain" and mtype == "Fire"):
        base = apply_mod(base, 2048)
    elif w == "sun" and move_id == "hydrosteam":
        base = apply_mod(base, 6144)
    if crit:
        base = math.floor(base * 1.5)

    # ---- Final modifiers ----
    stab = 4096
    if mtype in attacker.types or attacker.ability in ("protean", "libero"):
        stab = 8192 if attacker.ability == "adaptability" else 6144
    final: list[int] = []
    screens = state.sides[d_side]
    if not crit and attacker.ability != "infiltrator":
        if screens.auroraveil or (category == "Physical" and screens.reflect) or \
                (category == "Special" and screens.lightscreen):
            final.append(2732)
    if attacker.ability == "neuroforce" and eff > 1:
        final.append(5120)
    if attacker.ability == "sniper" and crit:
        final.append(6144)
    if attacker.ability == "tintedlens" and eff < 1:
        final.append(8192)
    if d_ability in ("multiscale", "shadowshield") and defender.hp == defender.maxhp:
        final.append(2048)
    if d_ability == "fluffy" and fl.get("contact") and attacker.item != "punchingglove":
        final.append(2048)
    if d_ability == "fluffy" and mtype == "Fire":
        final.append(8192)
    if d_ability == "punkrock" and fl.get("sound"):
        final.append(2048)
    if d_ability == "icescales" and category == "Special":
        final.append(2048)
    if d_ability in ("filter", "solidrock", "prismarmor") and eff > 1:
        final.append(3072)
    d_ally = _ally(state, d_side, defender)
    if d_ally is not None and d_ally.ability == "friendguard":
        final.append(3072)
    if attacker.item == "expertbelt" and eff > 1:
        final.append(4915)
    if attacker.item == "lifeorb":
        final.append(5324)
    berry = RESIST_BERRIES.get(defender.item)
    if berry == mtype and (eff > 1 or berry == "Normal") and attacker.ability not in ("unnerve", "asoneglastrier", "asonespectrier"):
        final.append(2048)
    final_mod = chain(final)

    hits = num_hits(attacker, move_id)
    burned = attacker.status == "brn" and category == "Physical" and attacker.ability != "guts" and move_id != "facade"
    out = []
    for i in range(16):
        d = math.floor(base * (85 + i) / 100)
        if stab != 4096:
            d = apply_mod(d, stab)
        d = math.floor(d * eff)
        if burned:
            d = math.floor(d / 2)
        d = max(1, apply_mod(d, final_mod))
        out.append(d)
    if hits != 1:
        # Each hit rolls separately, so the total's spread shrinks by sqrt(hits) around the mean.
        mean = sum(out) / 16
        out = [int(round(hits * (mean + (r - mean) / math.sqrt(hits)))) for r in out]
    return tuple(out)


def expected(rolls: Rolls) -> float:
    return sum(rolls) / 16.0


def ko_chance(rolls: Rolls, hp: int) -> float:
    return sum(1 for r in rolls if r >= hp) / 16.0


def accuracy(attacker: Mon, defender: Mon, move_id: str, state: State) -> float:
    md = move_data(move_id)
    acc = md.get("accuracy", True)
    if acc is True or attacker.ability == "noguard" or defender.ability == "noguard":
        return 1.0
    w = state.field.weather
    if move_id in ("thunder", "hurricane", "bleakwindstorm", "wildboltstorm", "sandsearstorm"):
        if w == "rain":
            return 1.0
        if w == "sun" and move_id in ("thunder", "hurricane"):
            acc = 50
    if move_id == "blizzard" and w == "snow":
        return 1.0
    acc = acc / 100.0
    stage = attacker.boosts["accuracy"] - (0 if attacker.ability == "unaware" else defender.boosts["evasion"])
    stage = max(-6, min(6, stage))
    acc *= (3 + stage) / 3 if stage >= 0 else 3 / (3 - stage)
    if attacker.ability == "compoundeyes":
        acc *= 1.3
    if attacker.ability == "hustle" and md["category"] == "Physical":
        acc *= 0.8
    if attacker.item == "widelens":
        acc *= 1.1
    if move_id == "tripleaxel":
        acc = acc * (1 + acc + acc * acc) / 3 if acc < 1 else 1.0  # expected share of the full 120 BP
    return min(1.0, acc)
