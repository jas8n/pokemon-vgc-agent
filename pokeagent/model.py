"""Battle state: a card's fixed set (Build), a Pokémon in battle (Mon), each side, and the field."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import dex
from .dex import to_id

STATS = ("hp", "atk", "def", "spa", "spd", "spe")
BOOSTABLE = ("atk", "def", "spa", "spd", "spe", "accuracy", "evasion")


def calc_stat(stat: str, base: int, iv: int, ev: int, level: int, nature: str | None) -> int:
    core = (2 * base + iv + ev // 4) * level // 100
    if stat == "hp":
        return 1 if base == 1 else core + level + 10
    return math.floor((core + 5) * dex.nature_mult(nature, stat))


def _stat_dict(raw: dict | None, default: int) -> dict:
    raw = raw or {}
    lookup = {to_id(k): v for k, v in raw.items()}
    aliases = {"hp": "hp", "atk": "atk", "def": "def", "spa": "spa", "spd": "spd", "spe": "spe",
               "attack": "atk", "defense": "def", "specialattack": "spa", "specialdefense": "spd", "speed": "spe",
               "spatk": "spa", "spdef": "spd"}
    out = {s: default for s in STATS}
    for key, value in lookup.items():
        if key in aliases and isinstance(value, (int, float)):
            out[aliases[key]] = int(value)
    return out


@dataclass
class Build:
    """A fixed set from the draft: everything about a Pokémon that never changes in battle."""

    card_id: str
    name: str
    sid: str
    types: tuple[str, ...]
    base: dict
    level: int
    nature: str
    evs: dict
    ivs: dict
    item: str
    ability: str
    moves: list[str]
    stats: dict
    weight: float

    @classmethod
    def from_card(cls, card: dict) -> "Build":
        entry = dex.species(card.get("species") or card.get("name") or card.get("card_id", ""))
        level = int(card.get("level") or 50)
        evs = _stat_dict(card.get("evs"), 0)
        ivs = _stat_dict(card.get("ivs"), 31)
        nature = card.get("nature") or "Serious"
        base = entry["baseStats"]
        stats = {s: calc_stat(s, base[s], ivs[s], evs[s], level, nature) for s in STATS}
        abilities = list(entry.get("abilities", {}).values())
        ability = to_id(card.get("ability")) or to_id(abilities[0] if abilities else "")
        moves = [to_id(m) for m in (card.get("moves") or []) if to_id(m) in dex.MOVES]
        return cls(
            card_id=str(card.get("card_id") or to_id(entry["name"])),
            name=entry["name"],
            sid=to_id(entry["name"]),
            types=tuple(entry["types"]),
            base=dict(base),
            level=level,
            nature=nature,
            evs=evs,
            ivs=ivs,
            item=to_id(card.get("item")),
            ability=ability,
            moves=moves,
            stats=stats,
            weight=float(entry.get("weightkg", 50.0)),
        )


@dataclass
class Mon:
    build: Build
    hp: int
    maxhp: int
    status: str = ""          # "", brn, par, psn, tox, slp, frz
    status_turns: int = 0     # sleep turns left / toxic counter
    boosts: dict = field(default_factory=lambda: {b: 0 for b in BOOSTABLE})
    item: str = ""
    ability: str = ""
    types: tuple = ()
    fainted: bool = False
    turns_out: int = 0        # 0 on the turn it came in (Fake Out / First Impression usable)
    protect_streak: int = 0
    choice_lock: str = ""
    vol: dict = field(default_factory=dict)  # volatile effects: taunt, encore, saltcure, leechseed, proto, ...
    revealed: bool = True     # opponent mons: seen in battle
    hits_taken: int = 0       # Rage Fist

    @classmethod
    def fresh(cls, build: Build) -> "Mon":
        return cls(build=build, hp=build.stats["hp"], maxhp=build.stats["hp"], item=build.item,
                   ability=build.ability, types=build.types)

    def copy(self) -> "Mon":
        m = Mon.__new__(Mon)
        m.__dict__.update(self.__dict__)
        m.boosts = dict(self.boosts)
        m.vol = dict(self.vol)
        return m

    @property
    def name(self) -> str:
        return self.build.name

    @property
    def sid(self) -> str:
        return self.build.sid

    @property
    def hp_frac(self) -> float:
        return 0.0 if self.fainted else self.hp / self.maxhp

    @property
    def alive(self) -> bool:
        return not self.fainted and self.hp > 0

    def has_type(self, t: str) -> bool:
        return dex.norm_type(t) in self.types

    def grounded(self, field_state: "Field | None" = None) -> bool:
        if field_state is not None and field_state.gravity:
            return True
        if self.item == "ironball":
            return True
        if self.has_type("Flying") or self.ability == "levitate" or self.item == "airballoon":
            return False
        return True

    def stat(self, s: str) -> int:
        return self.build.stats[s]

    def reset_on_switch_out(self) -> None:
        self.boosts = {b: 0 for b in BOOSTABLE}
        self.choice_lock = ""
        self.protect_streak = 0
        self.turns_out = 0
        keep = {k: v for k, v in self.vol.items() if k in ("proto_used",)}
        self.vol = keep
        if self.status == "tox":
            self.status_turns = 0
        if self.build.ability == "regenerator" and self.alive:
            self.hp = min(self.maxhp, self.hp + self.maxhp // 3)
        if self.build.ability == "naturalcure":
            self.status = ""
        self.ability = self.build.ability
        self.types = self.build.types


@dataclass
class Side:
    mons: list[Mon]
    active: list[int | None]  # indexes into mons for slot 0 / slot 1
    tailwind: int = 0
    reflect: int = 0
    lightscreen: int = 0
    auroraveil: int = 0
    safeguard: int = 0
    wideguard: bool = False
    quickguard: bool = False
    unseen: int = 0  # opponent Pokémon brought but not yet revealed (counted as full-HP unknowns)

    @property
    def fainted_count(self) -> int:
        return sum(1 for m in self.mons if m.fainted)

    def copy(self) -> "Side":
        s = Side.__new__(Side)
        s.__dict__.update(self.__dict__)
        s.mons = [m.copy() for m in self.mons]
        s.active = list(self.active)
        return s

    def active_mon(self, slot: int) -> Mon | None:
        idx = self.active[slot]
        if idx is None:
            return None
        mon = self.mons[idx]
        return mon if mon.alive else None

    def bench(self) -> list[int]:
        """Indexes of mons that could switch in (alive, not active, and part of the brought four)."""
        return [i for i, m in enumerate(self.mons) if m.alive and i not in self.active and not m.vol.get("not_brought")]

    def alive_count(self) -> int:
        return self.unseen + sum(1 for m in self.mons if m.alive and not m.vol.get("not_brought"))


@dataclass
class Field:
    weather: str = ""      # sun, rain, sand, snow (plus harshsun/heavyrain unlikely)
    weather_turns: int = 0
    terrain: str = ""      # electric, grassy, psychic, misty
    terrain_turns: int = 0
    trickroom: int = 0
    gravity: int = 0

    def copy(self) -> "Field":
        f = Field.__new__(Field)
        f.__dict__.update(self.__dict__)
        return f


@dataclass
class State:
    sides: list[Side]
    field: Field
    turn: int = 1

    def copy(self) -> "State":
        return State(sides=[s.copy() for s in self.sides], field=self.field.copy(), turn=self.turn)

    def actives(self):
        """(side, slot, mon) for every live active Pokémon."""
        for si, side in enumerate(self.sides):
            for slot in (0, 1):
                mon = side.active_mon(slot)
                if mon is not None:
                    yield si, slot, mon

    def winner(self) -> int | None:
        alive = [s.alive_count() for s in self.sides]
        if alive[0] == 0 and alive[1] == 0:
            return -1
        if alive[0] == 0:
            return 1
        if alive[1] == 0:
            return 0
        return None
