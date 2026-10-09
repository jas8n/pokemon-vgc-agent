"""What a card does for a team: roles read from its moves/ability/item, and 1v1 matchups."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from . import calc
from .calc import move_data
from .model import Build, Field, Mon, Side, State

SPEED_CONTROL = {"tailwind", "trickroom", "icywind", "electroweb", "thunderwave", "bleakwindstorm",
                 "glare", "nuzzle", "scaryface", "rocktomb", "bulldoze", "stringshot"}
REDIRECT = {"followme", "ragepowder", "spotlight"}
FAKE_OUT = {"fakeout"}
SLEEP = {"spore", "sleeppowder", "hypnosis", "yawn", "darkvoid", "lovelykiss"}
SUPPORT = {"helpinghand", "wideguard", "quickguard", "reflect", "lightscreen", "auroraveil", "coaching",
           "pollenpuff", "lifedew", "lunarblessing", "decorate", "partingshot", "taunt", "encore",
           "willowisp", "snarl", "haze", "clearsmog", "allyswitch", "imprison", "afteryou"}
PROTECT = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark", "obstruct"}

# Rough VGC strength prior (0-10) from Regulation G/H/I usage and results. Species missing here
# get a prior from their stats; the computed matchups do most of the work either way.
META_PRIOR = {
    "incineroar": 9.5, "urshifurapidstrike": 9, "urshifu": 8.5, "rillaboom": 8.5, "fluttermane": 9,
    "amoonguss": 8.5, "chiyu": 8, "chienpao": 8, "ogerponhearthflame": 8.5, "ogerponwellspring": 8.5,
    "ogerponcornerstone": 7.5, "ogerpon": 7, "tornadus": 8, "landorustherian": 7.5, "landorus": 8,
    "ironhands": 8, "farigiraf": 8, "indeedeef": 7.5, "ragingbolt": 8.5, "kingambit": 7.5, "gholdengo": 7.5,
    "ursalunabloodmoon": 7.5, "ursaluna": 7, "whimsicott": 7, "grimmsnarl": 7, "dragonite": 7,
    "archaludon": 12.0,  # take it first: every opposing agent did (12/12 pools); 87-73 and 86-74 offline
    "pelipper": 6.5, "torkoal": 7, "lilliganthisui": 6.5, "sneasler": 7,
    "annihilape": 6.5, "maushold": 6.5, "volcarona": 6.5, "ironboulder": 6.5, "ironcrown": 7,
    "gougingfire": 7, "walkingwake": 6.5, "ironbundle": 6.5, "porygon2": 6.5, "dondozo": 6,
    "tatsugiri": 6, "cresselia": 6.5, "hatterene": 6.5, "garchomp": 6, "gyarados": 6, "thundurus": 6.5,
    "basculegion": 6.5, "wochien": 5, "tinglu": 6.5, "glimmora": 6,
    "calyrexshadow": 10, "calyrexice": 9.5, "miraidon": 10, "koraidon": 9.5, "zacian": 9.5,
    "zaciancrowned": 10, "kyogre": 9.5, "groudon": 9.5, "terapagos": 9, "lunala": 9, "eternatus": 8,
    "zamazenta": 8, "zamazentacrowned": 8.5, "necrozmaduskmane": 8.5, "rayquaza": 8,
    "electabuzz": 2, "smeargle": 6, "murkrow": 5, "talonflame": 6, "kommoo": 6, "primarina": 6.5,
    "sinistcha": 7.5, "ironvaliant": 6, "greattusk": 6, "ironjugulis": 6, "ironmoth": 6, "ironleaves": 5.5,
    "baxcalibur": 6, "meowscarada": 5.5, "skeledirge": 5.5, "armarouge": 6, "pecharunt": 6.5,
    "okidogi": 6.5, "munkidao": 6, "fezandipiti": 6, "terapagosterastal": 9, "excadrill": 6,
    "tyranitar": 6, "kilowattrel": 5.5, "jumpluff": 5.5, "oranguru": 5.5, "mimikyu": 5.5,
    "arcaninehisui": 6.5, "arcanine": 6, "scizor": 5.5, "corviknight": 5, "hydreigon": 5.5,
    "goodra": 5, "goodrahisui": 5, "clefairy": 6.5, "dusclops": 6, "politoed": 6, "ninetalesalola": 6,
    "sylveon": 6.5, "salamence": 6.5, "sableye": 6, "klefki": 5.5, "milotic": 5.5, "lucario": 5,
}


@dataclass
class Roles:
    fake_out: bool
    intimidate: bool
    redirect: bool
    tailwind: bool
    trick_room: bool
    speed_control: bool
    protect: bool
    spread: int
    priority: bool
    sleep: bool
    support: int
    physical: bool
    special: bool
    weather: str
    terrain: str
    weather_abuser: str
    speed: int
    bulk: float


@lru_cache(maxsize=None)
def roles_of(build_key: str) -> Roles:
    b = _BUILDS[build_key]
    moves = set(b.moves)
    attacks = [m for m in b.moves if move_data(m)["category"] != "Status"]
    weather = {"drought": "sun", "orichalcumpulse": "sun", "drizzle": "rain", "sandstream": "sand",
               "snowwarning": "snow"}.get(b.ability, "")
    weather = weather or {"sunnyday": "sun", "raindance": "rain", "sandstorm": "sand", "snowscape": "snow"}.get(
        next((m for m in b.moves if m in ("sunnyday", "raindance", "sandstorm", "snowscape")), ""), "")
    abuser = {"chlorophyll": "sun", "solarpower": "sun", "protosynthesis": "sun", "swiftswim": "rain",
              "sandrush": "sand", "slushrush": "snow"}.get(b.ability, "")
    if not abuser and any(m in ("eruption", "heatwave", "flamethrower", "overheat") for m in b.moves) and b.ability != "drought":
        abuser = "sun?"
    if not abuser and any(m in ("electroshot", "hurricane", "thunder", "weatherball", "waterspout", "hydropump") for m in b.moves) \
            and b.ability != "drizzle":
        abuser = "rain?"
    terrain = {"electricsurge": "electric", "hadronengine": "electric", "grassysurge": "grassy",
               "psychicsurge": "psychic", "mistysurge": "misty"}.get(b.ability, "")
    return Roles(
        fake_out=bool(moves & FAKE_OUT),
        intimidate=b.ability == "intimidate",
        redirect=bool(moves & REDIRECT),
        tailwind="tailwind" in moves,
        trick_room="trickroom" in moves,
        speed_control=bool(moves & SPEED_CONTROL),
        protect=bool(moves & PROTECT),
        spread=sum(1 for m in attacks if calc.is_spread(m)),
        priority=any(move_data(m).get("priority", 0) > 0 for m in attacks) or
                 (b.ability == "prankster" and any(move_data(m)["category"] == "Status" for m in b.moves)),
        sleep=bool(moves & SLEEP),
        support=len(moves & SUPPORT) + len(moves & REDIRECT) + len(moves & FAKE_OUT),
        physical=any(move_data(m)["category"] == "Physical" for m in attacks),
        special=any(move_data(m)["category"] == "Special" for m in attacks),
        weather=weather,
        terrain=terrain,
        weather_abuser=abuser,
        speed=b.stats["spe"],
        bulk=b.stats["hp"] * (b.stats["def"] + b.stats["spd"]) / 2 / 10000,
    )


_BUILDS: dict[str, Build] = {}


def register(build: Build) -> str:
    key = build.card_id
    _BUILDS[key] = build
    return key


def prior(build: Build) -> float:
    if build.sid in META_PRIOR:
        return META_PRIOR[build.sid]
    bst = sum(build.base.values())
    return max(3.0, min(8.0, (bst - 400) / 40 + 3))


_EMPTY_FIELD = Field()


def _solo_state(a: Mon, b: Mon) -> State:
    return State([Side([a], [0, None]), Side([b], [0, None])], Field())


FULL_HP_SHIELDS = ("multiscale", "shadowshield")


@lru_cache(maxsize=None)
def best_hit(attacker_key: str, defender_key: str, trick_room: bool = False) -> tuple[float, bool, str]:
    """(fraction of defender HP from attacker's best attack, is_priority, move).

    Measured below full HP: Multiscale / Shadow Shield only halve the first hit, which `duel`
    accounts for separately, rather than every hit."""
    a, d = Mon.fresh(_BUILDS[attacker_key]), Mon.fresh(_BUILDS[defender_key])
    if d.ability in FULL_HP_SHIELDS:
        d.hp = d.maxhp - 1
    st = _solo_state(a, d)
    best, prio, best_move = 0.0, False, ""
    for m in a.build.moves:
        md = move_data(m)
        if md["category"] == "Status":
            continue
        rolls = calc.damage(st, 0, a, 1, d, m, spread=calc.is_spread(m))
        frac = calc.expected(rolls) / d.maxhp * calc.accuracy(a, d, m, st)
        if m in ("fakeout",):
            frac *= 0.5
        if m in ("electroshot", "meteorbeam", "solarbeam", "solarblade") and a.item != "powerherb":
            frac *= 0.5
        if frac > best:
            best, prio, best_move = frac, md.get("priority", 0) > 0, m
    return best, prio, best_move


@lru_cache(maxsize=None)
def duel(a_key: str, b_key: str) -> float:
    """1v1 score of a vs b in [-1, 1]: who knocks the other out first, counting speed."""
    fa, pa, _ = best_hit(a_key, b_key)
    fb, pb, _ = best_hit(b_key, a_key)
    ta = _hits_to_ko(fa, _BUILDS[b_key].ability in FULL_HP_SHIELDS)
    tb = _hits_to_ko(fb, _BUILDS[a_key].ability in FULL_HP_SHIELDS)
    sa, sb = _BUILDS[a_key].stats["spe"], _BUILDS[b_key].stats["spe"]
    a_first = sa > sb
    if ta == tb:
        if ta == 99:
            return 0.0
        return 0.6 if a_first else -0.6
    margin = tb - ta  # positive: a needs fewer hits
    return max(-1.0, min(1.0, 0.35 * margin + (0.15 if a_first else -0.15)))


def _hits_to_ko(frac: float, first_hit_halved: bool) -> int:
    if frac <= 0.01:
        return 99
    if not first_hit_halved:
        return math.ceil(1 / frac)
    rest = 1 - frac / 2
    return 1 + max(0, math.ceil(rest / frac - 1e-9))


def pressure(a_key: str, b_key: str) -> float:
    """How much of b's HP a removes per turn (capped at 1)."""
    return min(1.0, best_hit(a_key, b_key)[0])
