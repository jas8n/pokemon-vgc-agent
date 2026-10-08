"""Doubles turn resolver.

`resolve(state, actions, rng)` plays one turn and returns the new state. With `rng=None` it
runs in *expected* mode for search: damage is the average roll scaled by accuracy, a hit
knocks out when that's at least 50% likely, and effects only happen when they're at least
50% likely. With a `random.Random` it rolls everything, which is what offline games use.

Actions use the platform's encoding, relative to the acting side:
    ("move", move_id, target)   target 1/2 = foe slot 0/1, -1/-2 = own slot 0/1, 0 = none
    ("switch", mon_index)       index into side.mons
    ("pass",)
"""

from __future__ import annotations

import math
import random
from typing import Callable

from . import calc
from .calc import accuracy, damage, effective_speed, flags, move_data, move_type
from .model import Mon, State

Action = tuple
PROTECT_MOVES = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap",
                 "obstruct", "burningbulwark", "maxguard"}
REDIRECT_MOVES = {"followme": "followme", "ragepowder": "ragepowder", "spotlight": "followme"}
FIRST_TURN_ONLY = {"fakeout", "firstimpression", "matblock"}
SOUND_BYPASS = {"infiltrator"}
INTIMIDATE_IMMUNE = {"clearbody", "whitesmoke", "hypercutter", "innerfocus", "owntempo", "oblivious",
                     "scrappy", "fullmetalbody", "guarddog"}
WEATHER_ABILITIES = {"drought": "sun", "orichalcumpulse": "sun", "drizzle": "rain", "sandstream": "sand",
                     "snowwarning": "snow"}
TERRAIN_ABILITIES = {"electricsurge": "electric", "hadronengine": "electric", "grassysurge": "grassy",
                     "psychicsurge": "psychic", "mistysurge": "misty", "seedsower": None}
TERRAIN_SEEDS = {"electricseed": ("electric", "def"), "grassyseed": ("grassy", "def"),
                 "psychicseed": ("psychic", "spd"), "mistyseed": ("misty", "spd")}
WEATHER_MOVES = {"sunnyday": "sun", "raindance": "rain", "sandstorm": "sand", "snowscape": "snow",
                 "chillyreception": "snow"}
WEATHER_ROCKS = {"sun": "heatrock", "rain": "damprock", "sand": "smoothrock", "snow": "icyrock"}
TERRAIN_MOVES = {"electricterrain": "electric", "grassyterrain": "grassy", "psychicterrain": "psychic",
                 "mistyterrain": "misty"}
STATUS_IMMUNE_TYPES = {"brn": ("Fire",), "par": ("Electric",), "psn": ("Poison", "Steel"),
                       "tox": ("Poison", "Steel"), "frz": ("Ice",)}
STATUS_IMMUNE_ABILITIES = {"slp": {"insomnia", "vitalspirit", "sweetveil", "comatose", "purifyingsalt"},
                           "brn": {"waterveil", "waterbubble", "thermalexchange", "purifyingsalt", "comatose"},
                           "par": {"limber", "purifyingsalt", "comatose"},
                           "psn": {"immunity", "pastelveil", "purifyingsalt", "comatose"},
                           "tox": {"immunity", "pastelveil", "purifyingsalt", "comatose"},
                           "frz": {"magmaarmor", "purifyingsalt", "comatose"}}
CONTACT_PUNISH = {"roughskin": 8, "ironbarbs": 8}
BERRY_HEAL = {"sitrusberry": 4}
PINCH_BERRIES = {"figyberry", "wikiberry", "magoberry", "aguavberry", "iapapaberry"}


def _boost(mon: Mon, stat: str, amount: int, state: State, source_foe: bool = False) -> int:
    """Apply a stat change with the usual ability/item interactions; returns the change applied."""
    if not mon.alive or amount == 0:
        return 0
    if amount < 0 and source_foe:
        if mon.ability in ("clearbody", "whitesmoke", "fullmetalbody") or mon.item == "clearamulet":
            return 0
        if mon.ability == "hypercutter" and stat == "atk":
            return 0
        if mon.ability == "bigpecks" and stat == "def":
            return 0
        if mon.ability == "keeneye" and stat == "accuracy":
            return 0
    if mon.ability == "contrary":
        amount = -amount
    if mon.ability == "simple":
        amount *= 2
    old = mon.boosts[stat]
    mon.boosts[stat] = max(-6, min(6, old + amount))
    applied = mon.boosts[stat] - old
    if applied < 0 and source_foe:
        if mon.ability == "defiant":
            mon.boosts["atk"] = min(6, mon.boosts["atk"] + 2)
        elif mon.ability == "competitive":
            mon.boosts["spa"] = min(6, mon.boosts["spa"] + 2)
        if mon.item == "whiteherb":
            for k in mon.boosts:
                mon.boosts[k] = max(0, mon.boosts[k])
            mon.item = ""
    return applied


def _heal(mon: Mon, amount: int) -> None:
    if mon.alive and amount > 0 and not mon.vol.get("healblock"):
        mon.hp = min(mon.maxhp, mon.hp + amount)


def _chip(mon: Mon, amount: int) -> None:
    """Indirect damage (recoil, weather, status)."""
    if mon.alive and amount > 0 and mon.ability != "magicguard":
        mon.hp -= amount
        if mon.hp <= 0:
            mon.hp, mon.fainted = 0, True


def can_status(mon: Mon, status: str, state: State, side_idx: int, by_foe: bool = True) -> bool:
    if not mon.alive or mon.status:
        return False
    if any(t in mon.types for t in STATUS_IMMUNE_TYPES.get(status, ())):
        return False
    if mon.ability in STATUS_IMMUNE_ABILITIES.get(status, set()):
        return False
    if by_foe and state.sides[side_idx].safeguard:
        return False
    if mon.grounded(state.field):
        if state.field.terrain == "misty":
            return False
        if status == "slp" and state.field.terrain == "electric":
            return False
    if status == "slp" and mon.vol.get("_sleep_clause"):
        return False
    return True


def _set_status(mon: Mon, status: str, rng: random.Random | None) -> None:
    mon.status = status
    if status == "slp":
        mon.status_turns = rng.randint(1, 3) if rng else 2
    elif status == "tox":
        mon.status_turns = 0
    if mon.item == "lumberry" or (mon.item == "chestoberry" and status == "slp"):
        mon.status, mon.item = "", ""


class Resolver:
    """One turn of a doubles battle."""

    def __init__(self, state: State, rng: random.Random | None,
                 replace: Callable[[State, int, int], int | None] | None = None):
        self.s = state
        self.rng = rng
        self.replace = replace
        self.moved: set[tuple[int, int]] = set()
        self.actions: dict[tuple[int, int], Action] = {}
        self.log: list[str] = []

    # ---- helpers ----
    def chance(self, p: float) -> bool:
        if p >= 1:
            return True
        if p <= 0:
            return False
        if self.rng is None:
            return p >= 0.5
        return self.rng.random() < p

    def mon_at(self, side: int, slot: int) -> Mon | None:
        return self.s.sides[side].active_mon(slot)

    def foes(self, side: int) -> list[tuple[int, int, Mon]]:
        return [(1 - side, sl, m) for sl in (0, 1) if (m := self.mon_at(1 - side, sl)) is not None]

    def ally(self, side: int, slot: int) -> tuple[int, int, Mon] | None:
        m = self.mon_at(side, 1 - slot)
        return (side, 1 - slot, m) if m is not None else None

    def priority(self, side: int, slot: int, action: Action) -> float:
        if action[0] == "switch":
            return 7
        if action[0] != "move":
            return -99
        mon = self.mon_at(side, slot)
        mid = action[1]
        md = move_data(mid)
        p = md.get("priority", 0)
        if mon is not None:
            if mon.ability == "prankster" and md["category"] == "Status":
                p += 1
            if mon.ability == "galewings" and md["type"] == "Flying" and mon.hp == mon.maxhp:
                p += 1
            if mon.ability == "triage" and (md.get("heal") or md.get("drain")):
                p += 3
            if mid == "grassyglide" and self.s.field.terrain == "grassy" and mon.grounded(self.s.field):
                p += 1
        return p

    def order_key(self, side: int, slot: int, action: Action) -> tuple:
        mon = self.mon_at(side, slot)
        spe = effective_speed(mon, self.s, side) if mon is not None else 0
        if self.s.field.trickroom and action[0] == "move":
            spe = 10000 - spe
        tiebreak = self.rng.random() if self.rng else (0.5 if side == 0 else 0.4)
        lagging = 1 if (mon is not None and mon.item in ("laggingtail", "fullincense")) else 0
        return (-self.priority(side, slot, action), lagging, -spe, tiebreak)

    # ---- entry effects ----
    def on_entry(self, side: int, slot: int) -> None:
        mon = self.mon_at(side, slot)
        if mon is None:
            return
        ab = mon.ability
        f = self.s.field
        if ab in WEATHER_ABILITIES:
            w = WEATHER_ABILITIES[ab]
            if f.weather != w:
                f.weather = w
                f.weather_turns = 8 if mon.item == WEATHER_ROCKS.get(w) else 5
        if ab in TERRAIN_ABILITIES and TERRAIN_ABILITIES[ab]:
            t = TERRAIN_ABILITIES[ab]
            if f.terrain != t:
                f.terrain = t
                f.terrain_turns = 8 if mon.item == "terrainextender" else 5
        if ab == "intimidate":
            for fs, fsl, foe in self.foes(side):
                if foe.ability in INTIMIDATE_IMMUNE or foe.item == "clearamulet":
                    if foe.ability == "guarddog":
                        _boost(foe, "atk", 1, self.s)
                    continue
                if foe.ability == "mirrorarmor":
                    _boost(mon, "atk", -1, self.s, source_foe=True)
                    continue
                _boost(foe, "atk", -1, self.s, source_foe=True)
                if foe.ability == "rattled":
                    _boost(foe, "spe", 1, self.s)
                if foe.item == "adrenalineorb":
                    _boost(foe, "spe", 1, self.s)
                    foe.item = ""
        if ab in ("protosynthesis", "quarkdrive") and mon.item == "boosterenergy" and not mon.vol.get("proto"):
            natural = (ab == "protosynthesis" and f.weather == "sun") or (ab == "quarkdrive" and f.terrain == "electric")
            if not natural:
                mon.vol["proto"] = True
                mon.item = ""
        if ab == "download":
            foes = [m for _, _, m in self.foes(side)]
            if foes:
                d = sum(m.stat("def") for m in foes)
                sd = sum(m.stat("spd") for m in foes)
                _boost(mon, "spa" if sd <= d else "atk", 1, self.s)
        self.check_seeds()

    def check_seeds(self) -> None:
        for _, _, m in self.s.actives():
            seed = TERRAIN_SEEDS.get(m.item)
            if seed and seed[0] == self.s.field.terrain:
                _boost(m, seed[1], 1, self.s)
                m.item = ""

    def switch_in(self, side: int, slot: int, idx: int) -> None:
        sd = self.s.sides[side]
        old = sd.active[slot]
        if old is not None and sd.mons[old].alive:
            sd.mons[old].reset_on_switch_out()
        sd.active[slot] = idx
        mon = sd.mons[idx]
        mon.turns_out = 0
        mon.revealed = True
        self.log.append(f"p{side + 1} {mon.name} in")
        self.on_entry(side, slot)

    # ---- the turn ----
    def run(self, actions: list[list[Action]]) -> State:
        s = self.s
        queue = []
        for side in (0, 1):
            for slot in (0, 1):
                a = actions[side][slot] if slot < len(actions[side]) else ("pass",)
                if a[0] == "pass" or self.s.sides[side].active[slot] is None:
                    continue
                if a[0] != "switch" and self.mon_at(side, slot) is None:
                    continue
                self.actions[(side, slot)] = a
                queue.append((side, slot, a))
        # Protect/redirection/Helping Hand style flags are cleared each turn
        for _, _, m in s.actives():
            for k in ("protect", "followme", "ragepowder", "helpinghand", "flinch", "moved", "endure"):
                m.vol.pop(k, None)
        for sd in s.sides:
            sd.wideguard = sd.quickguard = False

        while queue:
            queue.sort(key=lambda q: self.order_key(*q))
            side, slot, action = queue.pop(0)
            if action[0] == "switch":
                idx = action[1]
                if self.s.sides[side].mons[idx].alive and idx not in self.s.sides[side].active:
                    self.switch_in(side, slot, idx)
                continue
            mon = self.mon_at(side, slot)
            if mon is None:
                continue
            self.use_move(side, slot, mon, action[1], action[2] if len(action) > 2 else 0)
            mon.vol["moved"] = True
            self.moved.add((side, slot))
            if s.winner() is not None:
                break
        self.end_of_turn()
        s.turn += 1
        return s

    # ---- moves ----
    def resolve_targets(self, side: int, slot: int, mon: Mon, move_id: str, target: int) -> list[tuple[int, int, Mon]]:
        md = move_data(move_id)
        kind = md.get("target", "normal")
        if kind in ("allAdjacentFoes",):
            return self.foes(side)
        if kind == "allAdjacent":
            out = self.foes(side)
            a = self.ally(side, slot)
            return out + ([a] if a else [])
        if kind in ("self", "allySide", "foeSide", "all", "allyTeam", "allies"):
            return [(side, slot, mon)]
        if kind in ("adjacentAlly",):
            a = self.ally(side, slot)
            return [a] if a else []
        if kind == "adjacentAllyOrSelf":
            if target in (-1, -2) and -target - 1 != slot:
                a = self.ally(side, slot)
                return [a] if a else [(side, slot, mon)]
            return [(side, slot, mon)]
        if kind == "randomNormal":
            foes = self.foes(side)
            if not foes:
                return []
            return [foes[self.rng.randrange(len(foes))]] if self.rng else [foes[0]]
        # single target ("normal", "any", "adjacentFoe")
        if target in (-1, -2):
            tslot = -target - 1
            t = self.mon_at(side, tslot)
            return [(side, tslot, t)] if t is not None and tslot != slot else []
        tslot = (target - 1) if target in (1, 2) else 0
        foe_side = 1 - side
        # Redirection (Follow Me / Rage Powder / Lightning Rod / Storm Drain)
        if not (mon.ability in ("stalwart", "propellertail") or move_id in ("snipeshot",)):
            for fsl in (0, 1):
                f = self.mon_at(foe_side, fsl)
                if f is None:
                    continue
                if f.vol.get("followme") or (f.vol.get("ragepowder") and not (
                        mon.has_type("Grass") or mon.ability == "overcoat" or mon.item == "safetygoggles")):
                    return [(foe_side, fsl, f)]
            mtype = move_type(mon, move_id, self.s)
            for fsl in (0, 1):
                f = self.mon_at(foe_side, fsl)
                if f is not None and ((f.ability == "lightningrod" and mtype == "Electric") or
                                      (f.ability == "stormdrain" and mtype == "Water")):
                    return [(foe_side, fsl, f)]
        t = self.mon_at(foe_side, tslot)
        if t is None:
            t = self.mon_at(foe_side, 1 - tslot)
            tslot = 1 - tslot
        return [(foe_side, tslot, t)] if t is not None else []

    def use_move(self, side: int, slot: int, mon: Mon, move_id: str, target: int) -> None:
        md = move_data(move_id)
        s = self.s
        if mon.vol.get("flinch"):
            return
        if mon.status == "slp":
            mon.status_turns -= 1
            if mon.status_turns > 0:
                return
            mon.status = ""
        if mon.status == "frz":
            if self.rng and self.rng.random() < 0.2 or not self.rng:
                mon.status = ""
            else:
                return
        if mon.status == "par" and self.rng is not None and self.rng.random() < 0.25:
            return
        if move_id in FIRST_TURN_ONLY and mon.turns_out > 0:
            mon.vol["last_failed"] = True
            return
        if mon.item in ("choiceband", "choicespecs", "choicescarf") or mon.ability == "gorillatactics":
            if not mon.choice_lock:
                mon.choice_lock = move_id
        mon.vol["lastmove"] = move_id
        if mon.vol.get("encore"):
            move_id = mon.vol.get("encoremove", move_id)
            md = move_data(move_id)
        mon.vol.pop("last_failed", None)

        # Protection and similar self-targeting volatile moves
        if move_id in PROTECT_MOVES or move_id == "endure":
            p = 1.0 / (3 ** mon.protect_streak)
            if self.chance(p) and any(q for q in self.pending(side, slot)):
                mon.vol["protect" if move_id != "endure" else "endure"] = move_id
                mon.protect_streak += 1
            else:
                mon.protect_streak = 0
                mon.vol["last_failed"] = True
            return
        mon.protect_streak = 0
        if move_id == "pollenpuff" and target in (-1, -2) and -target - 1 != slot:
            a = self.ally(side, slot)
            if a is not None and a[2].alive:
                _heal(a[2], a[2].maxhp // 2)
            return
        if move_id in REDIRECT_MOVES:
            mon.vol[REDIRECT_MOVES[move_id]] = True
            return
        if move_id == "helpinghand":
            a = self.ally(side, slot)
            if a is not None and a[:2] not in self.moved:
                a[2].vol["helpinghand"] = True
            return
        if move_id in ("wideguard", "quickguard"):
            setattr(s.sides[side], move_id, True)
            return
        if move_id in ("suckerpunch", "thunderclap"):
            tgt = self.resolve_targets(side, slot, mon, move_id, target)
            if not tgt:
                return
            ts, tsl, _ = tgt[0]
            ta = self.actions.get((ts, tsl))
            if (ts, tsl) in self.moved or ta is None or ta[0] != "move" or \
                    move_data(ta[1])["category"] == "Status":
                mon.vol["last_failed"] = True
                return
        if md.get("category") == "Status":
            self.status_move(side, slot, mon, move_id, target)
            return
        # Charge moves
        if move_id in ("electroshot", "meteorbeam") or move_id in ("solarbeam", "solarblade"):
            if move_id == "electroshot":
                _boost(mon, "spa", 1, s)
                if s.field.weather != "rain" and mon.item != "powerherb":
                    return
            elif move_id == "meteorbeam":
                _boost(mon, "spa", 1, s)
                if mon.item != "powerherb":
                    return
            elif s.field.weather != "sun" and mon.item != "powerherb":
                return
            if mon.item == "powerherb" and not (move_id == "electroshot" and s.field.weather == "rain"):
                mon.item = ""
        self.attack(side, slot, mon, move_id, target)

    def pending(self, side: int, slot: int) -> list:
        """Whether anything still has to move this turn (Protect fails if it moves last)."""
        return [k for k in self.actions if k not in self.moved and k != (side, slot)]

    def attack(self, side: int, slot: int, mon: Mon, move_id: str, target: int) -> None:
        s = self.s
        md = move_data(move_id)
        fl = flags(move_id)
        targets = self.resolve_targets(side, slot, mon, move_id, target)
        if not targets:
            mon.vol["last_failed"] = True
            return
        spread = len(targets) > 1
        mtype = move_type(mon, move_id, s)
        priority = self.priority(side, slot, ("move", move_id))
        total_dealt = 0
        any_hit = False
        helping = bool(mon.vol.get("helpinghand"))
        sheer = mon.ability == "sheerforce" and (md.get("secondary") or md.get("secondaries"))
        for ts, tsl, tgt in targets:
            if not tgt.alive:
                continue
            tside = s.sides[ts]
            if ts != side:
                if tgt.vol.get("protect") and (fl.get("protect") or move_id in ("feint",)):
                    unseen = mon.ability == "unseenfist" and fl.get("contact")
                    if not unseen:
                        self.protect_contact(mon, tgt, fl, side)
                        continue
                if spread and tside.wideguard:
                    continue
                if priority > 0 and tside.quickguard:
                    continue
            # Ability absorbs
            if ts != side or md.get("target") == "allAdjacent":
                absorbed = self.absorb(tgt, mtype, mon)
                if absorbed:
                    continue
            acc = 1.0 if ts == side and md.get("target") != "allAdjacent" else accuracy(mon, tgt, move_id, s)
            if self.rng is not None:
                if self.rng.random() >= acc:
                    continue
                crit_rate = {0: 1 / 24, 1: 1 / 8, 2: 1 / 2}.get(
                    (md.get("critRatio", 1) - 1) + (1 if mon.item in ("scopelens", "razorclaw") else 0)
                    + (2 if mon.vol.get("focusenergy") else 0), 1.0)
                crit = self.rng.random() < crit_rate
                rolls = damage(s, side, mon, ts, tgt, move_id, spread=spread, crit=crit, helping=helping,
                               moved_first=(ts, tsl) not in self.moved)
                dmg = rolls[self.rng.randrange(16)]
            else:
                rolls = damage(s, side, mon, ts, tgt, move_id, spread=spread, helping=helping,
                               moved_first=(ts, tsl) not in self.moved)
                p_ko = acc * calc.ko_chance(rolls, tgt.hp)
                if p_ko >= 0.5:
                    dmg = tgt.hp
                else:
                    survivors = [r for r in rolls if r < tgt.hp] or [tgt.hp - 1]
                    dmg = int(acc * sum(survivors) / len(survivors))
            if dmg <= 0:
                continue
            any_hit = True
            dealt = self.deal(tgt, dmg, ts, mon, move_id)
            total_dealt += dealt
            if ts != side:
                self.after_hit(side, mon, ts, tgt, move_id, mtype, dealt, sheer)
        if any_hit:
            self.after_attack(side, slot, mon, move_id, total_dealt, sheer)
        else:
            mon.vol["last_failed"] = True

    def protect_contact(self, attacker: Mon, protector: Mon, fl: dict, side: int) -> None:
        if not fl.get("contact") or attacker.item == "protectivepads" or attacker.item == "punchingglove" and fl.get("punch"):
            return
        kind = protector.vol.get("protect")
        if kind == "spikyshield":
            _chip(attacker, attacker.maxhp // 8)
        elif kind == "kingsshield":
            _boost(attacker, "atk", -1, self.s, source_foe=True)
        elif kind == "silktrap":
            _boost(attacker, "spe", -1, self.s, source_foe=True)
        elif kind == "obstruct":
            _boost(attacker, "def", -2, self.s, source_foe=True)
        elif kind == "banefulbunker" and can_status(attacker, "psn", self.s, side):
            _set_status(attacker, "psn", self.rng)
        elif kind == "burningbulwark" and can_status(attacker, "brn", self.s, side):
            _set_status(attacker, "brn", self.rng)

    def absorb(self, tgt: Mon, mtype: str, attacker: Mon) -> bool:
        if calc.defender_ability_ignored(attacker, "", tgt):
            return False
        ab = tgt.ability
        if ab in ("waterabsorb", "dryskin") and mtype == "Water" or ab == "voltabsorb" and mtype == "Electric" \
                or ab == "eartheater" and mtype == "Ground":
            _heal(tgt, tgt.maxhp // 4)
            return True
        if ab == "lightningrod" and mtype == "Electric" or ab == "stormdrain" and mtype == "Water":
            _boost(tgt, "spa", 1, self.s)
            return True
        if ab == "sapsipper" and mtype == "Grass":
            _boost(tgt, "atk", 1, self.s)
            return True
        if ab == "motordrive" and mtype == "Electric":
            _boost(tgt, "spe", 1, self.s)
            return True
        if ab == "flashfire" and mtype == "Fire":
            tgt.vol["flashfire"] = True
            return True
        if ab == "wellbakedbody" and mtype == "Fire":
            _boost(tgt, "def", 2, self.s)
            return True
        return False

    def deal(self, tgt: Mon, dmg: int, ts: int, attacker: Mon, move_id: str) -> int:
        dmg = min(dmg, tgt.hp)
        if dmg >= tgt.hp and tgt.hp == tgt.maxhp and (tgt.item == "focussash" or tgt.ability == "sturdy") \
                and calc.num_hits(attacker, move_id) == 1:
            dmg = tgt.hp - 1
            if tgt.item == "focussash":
                tgt.item = ""
        if dmg >= tgt.hp and tgt.vol.get("endure"):
            dmg = tgt.hp - 1
        tgt.hp -= dmg
        tgt.hits_taken += max(1, int(round(calc.num_hits(attacker, move_id))))
        if tgt.hp <= 0:
            tgt.hp, tgt.fainted = 0, True
            self.log.append(f"p{ts + 1} {tgt.name} fainted")
        return dmg

    def after_hit(self, side: int, mon: Mon, ts: int, tgt: Mon, move_id: str, mtype: str, dealt: int,
                  sheer: bool) -> None:
        s = self.s
        md = move_data(move_id)
        fl = flags(move_id)
        hits = max(1, int(round(calc.num_hits(mon, move_id))))
        contact = fl.get("contact") and mon.item != "protectivepads" and not (
            mon.item == "punchingglove" and fl.get("punch"))
        if tgt.alive:
            eff = calc.effectiveness(mon, tgt, move_id, mtype, s)
            if tgt.item == "weaknesspolicy" and eff > 1:
                _boost(tgt, "atk", 2, s)
                _boost(tgt, "spa", 2, s)
                tgt.item = ""
            if tgt.item in BERRY_HEAL and tgt.hp * 2 <= tgt.maxhp:
                _heal(tgt, tgt.maxhp // BERRY_HEAL[tgt.item])
                tgt.item = ""
            if tgt.item in PINCH_BERRIES and tgt.hp * 4 <= tgt.maxhp:
                _heal(tgt, tgt.maxhp // 3)
                tgt.item = ""
            if tgt.item in calc.RESIST_BERRIES and calc.RESIST_BERRIES[tgt.item] == mtype and eff > 1:
                tgt.item = ""
            # These trigger on every hit of a multi-hit move (Scale Shot into Stamina is +5 Defense)
            if tgt.ability == "stamina":
                _boost(tgt, "def", hits, s)
            if tgt.ability == "justified" and mtype == "Dark":
                _boost(tgt, "atk", hits, s)
            if tgt.ability == "weakarmor" and md["category"] == "Physical":
                _boost(tgt, "def", -hits, s)
                _boost(tgt, "spe", 2 * hits, s)
            if tgt.ability in ("steamengine",) and mtype in ("Fire", "Water"):
                _boost(tgt, "spe", 6, s)
            if tgt.ability == "thermalexchange" and mtype == "Fire":
                _boost(tgt, "atk", hits, s)
            if tgt.ability == "angershell" and tgt.hp * 2 <= tgt.maxhp < (tgt.hp + dealt) * 2:
                for st, v in (("atk", 1), ("spa", 1), ("spe", 1), ("def", -1), ("spd", -1)):
                    _boost(tgt, st, v, s)
            if tgt.ability in ("electromorphosis", "windpower"):
                tgt.vol["charge"] = True
            if tgt.ability == "seedsower":
                s.field.terrain, s.field.terrain_turns = "grassy", 5
            if tgt.ability == "cottondown":
                for _, _, other in s.actives():
                    if other is not tgt:
                        _boost(other, "spe", -1, s, source_foe=True)
        if contact:
            if tgt.item == "rockyhelmet":
                _chip(mon, hits * (mon.maxhp // 6))
            if tgt.ability in CONTACT_PUNISH:
                _chip(mon, hits * (mon.maxhp // CONTACT_PUNISH[tgt.ability]))
            if tgt.ability == "flamebody" and self.chance(0.3) and can_status(mon, "brn", s, side):
                _set_status(mon, "brn", self.rng)
            if tgt.ability == "static" and self.chance(0.3) and can_status(mon, "par", s, side):
                _set_status(mon, "par", self.rng)
            if tgt.ability == "poisonpoint" and self.chance(0.3) and can_status(mon, "psn", s, side):
                _set_status(mon, "psn", self.rng)
            if tgt.ability == "gooey" or tgt.ability == "tanglinghair":
                _boost(mon, "spe", -1, s, source_foe=True)
        if move_id == "knockoff" and tgt.item and not tgt.item.endswith("mask") and tgt.alive:
            tgt.item = ""
        # Secondary effects
        if sheer or tgt.item == "covertcloak" or tgt.ability == "shielddust":
            return
        secs = md.get("secondaries") or ([md["secondary"]] if md.get("secondary") else [])
        for sec in secs:
            chance = (sec.get("chance") or 100) / 100.0
            if mon.ability == "serenegrace":
                chance = min(1.0, chance * 2)
            if not self.chance(chance):
                continue
            if sec.get("boosts") and tgt.alive:
                for st, v in sec["boosts"].items():
                    _boost(tgt, st, v, s, source_foe=True)
            if sec.get("status") and can_status(tgt, sec["status"], s, ts):
                _set_status(tgt, sec["status"], self.rng)
            if sec.get("volatileStatus") == "flinch" and tgt.alive and tgt.ability != "innerfocus":
                tgt.vol["flinch"] = True
            if sec.get("volatileStatus") == "saltcure":
                tgt.vol["saltcure"] = True
            if sec.get("self") and sec["self"].get("boosts"):
                for st, v in sec["self"]["boosts"].items():
                    _boost(mon, st, v, s)

    def after_attack(self, side: int, slot: int, mon: Mon, move_id: str, total: int, sheer: bool) -> None:
        s = self.s
        md = move_data(move_id)
        if md.get("drain") and total:
            n, d = md["drain"]
            _heal(mon, max(1, total * n // d))
        if md.get("recoil") and total and mon.ability not in ("rockhead", "magicguard"):
            n, d = md["recoil"]
            _chip(mon, max(1, total * n // d))
        if md.get("mindBlownRecoil") or move_id in ("mindblown", "steelbeam"):
            _chip(mon, math.ceil(mon.maxhp / 2))
        if mon.item == "lifeorb" and total and not sheer:
            _chip(mon, mon.maxhp // 10)
        if mon.item == "shellbell" and total:
            _heal(mon, total // 8)
        selfeff = md.get("self") or {}
        if selfeff.get("boosts"):
            for st, v in selfeff["boosts"].items():
                _boost(mon, st, v, s)
        if md.get("selfBoost") and md["selfBoost"].get("boosts"):
            for st, v in md["selfBoost"]["boosts"].items():
                _boost(mon, st, v, s)
        if move_id in ("closecombat", "headlongrush", "armorcannon"):
            pass  # covered by "self" boosts in the data
        if move_id == "throatchop":
            pass
        if mon.item == "throatspray" and flags(move_id).get("sound"):
            _boost(mon, "spa", 1, s)
            mon.item = ""
        if mon.alive and md.get("selfSwitch") and self.replace is not None and self.rng is not None:
            bench = s.sides[side].bench()
            if bench:
                choice = self.replace(s, side, slot)
                if choice is not None:
                    self.switch_in(side, slot, choice)

    def status_move(self, side: int, slot: int, mon: Mon, move_id: str, target: int) -> None:
        s = self.s
        md = move_data(move_id)
        sd = s.sides[side]
        f = s.field
        if move_id == "trickroom":
            f.trickroom = 0 if f.trickroom else 5
            return
        if move_id == "gravity":
            f.gravity = 5
            return
        if move_id in WEATHER_MOVES:
            w = WEATHER_MOVES[move_id]
            f.weather, f.weather_turns = w, 8 if mon.item == WEATHER_ROCKS.get(w) else 5
            if move_id == "chillyreception" and self.replace and self.rng is not None and sd.bench():
                c = self.replace(s, side, slot)
                if c is not None:
                    self.switch_in(side, slot, c)
            return
        if move_id in TERRAIN_MOVES:
            f.terrain, f.terrain_turns = TERRAIN_MOVES[move_id], 8 if mon.item == "terrainextender" else 5
            self.check_seeds()
            return
        sc = md.get("sideCondition")
        if sc:
            light_clay = mon.item == "lightclay"
            if sc == "tailwind":
                if not sd.tailwind:
                    sd.tailwind = 4
                    for _, _, a in s.actives():
                        if a.ability == "windrider" and a in [x for _, _, x in s.actives()]:
                            pass
            elif sc == "reflect":
                sd.reflect = sd.reflect or (8 if light_clay else 5)
            elif sc == "lightscreen":
                sd.lightscreen = sd.lightscreen or (8 if light_clay else 5)
            elif sc == "auroraveil":
                if f.weather == "snow":
                    sd.auroraveil = sd.auroraveil or (8 if light_clay else 5)
            elif sc == "safeguard":
                sd.safeguard = 5
            return
        if move_id in ("haze",):
            for _, _, a in s.actives():
                a.boosts = {k: 0 for k in a.boosts}
            return
        # Self or ally boosting / healing
        targets = self.resolve_targets(side, slot, mon, move_id, target)
        heal = md.get("heal")
        for ts, tsl, tgt in targets:
            if ts != side:
                if tgt.vol.get("protect") and flags(move_id).get("protect"):
                    continue
                if mon.ability == "prankster" and tgt.has_type("Dark"):
                    continue
                if tgt.ability == "goodasgold":
                    continue
                if tgt.ability == "magicbounce" and flags(move_id).get("reflectable"):
                    continue
                if flags(move_id).get("powder") and (tgt.has_type("Grass") or tgt.ability == "overcoat"
                                                    or tgt.item == "safetygoggles"):
                    continue
                if md.get("accuracy") is not True and self.rng is not None and \
                        self.rng.random() >= accuracy(mon, tgt, move_id, s):
                    continue
            if md.get("status"):
                st = md["status"]
                if st in ("par",) and move_id == "thunderwave" and tgt.has_type("Ground"):
                    continue
                if can_status(tgt, st, s, ts, by_foe=ts != side):
                    _set_status(tgt, st, self.rng)
            if md.get("boosts"):
                for st, v in md["boosts"].items():
                    _boost(tgt, st, v, s, source_foe=ts != side)
            vs = md.get("volatileStatus")
            if vs == "taunt" and tgt.ability != "oblivious":
                tgt.vol["taunt"] = 3
            elif vs == "encore" and tgt.vol.get("lastmove"):
                tgt.vol["encore"], tgt.vol["encoremove"] = 3, tgt.vol["lastmove"]
            elif vs == "yawn" and can_status(tgt, "slp", s, ts):
                tgt.vol.setdefault("yawn", 2)
            elif vs == "leechseed" and not tgt.has_type("Grass"):
                tgt.vol["leechseed"] = (side, slot)
            elif vs == "focusenergy":
                tgt.vol["focusenergy"] = True
            elif vs == "charge":
                tgt.vol["charge"] = True
            elif vs == "substitute" and tgt.hp * 4 > tgt.maxhp:
                _chip(tgt, tgt.maxhp // 4)
            if heal:
                n, d = heal
                frac = n / d
                if move_id in ("moonlight", "synthesis", "morningsun"):
                    frac = {"sun": 2 / 3, "": 0.5}.get(f.weather, 0.25)
                if move_id == "shoreup" and f.weather == "sand":
                    frac = 2 / 3
                _heal(tgt, math.floor(tgt.maxhp * frac))
            if move_id == "strengthsap" and ts != side:
                atk = math.floor(tgt.stat("atk") * calc.BOOST_TABLE[tgt.boosts["atk"]])
                _heal(mon, atk)
                _boost(tgt, "atk", -1, s, source_foe=True)
            if move_id in ("trick", "switcheroo") and ts != side:
                if not (tgt.item.endswith("mask") or mon.item.endswith("mask")):
                    mon.item, tgt.item = tgt.item, mon.item
                    if tgt.item.startswith("choice"):
                        tgt.choice_lock = tgt.vol.get("lastmove", "")
        if move_id in ("lifedew", "lunarblessing", "jungleheal"):
            for ts, tsl, a in [(side, sl, m) for sl in (0, 1) if (m := self.mon_at(side, sl)) is not None]:
                _heal(a, a.maxhp // 4)
                if move_id != "lifedew":
                    a.status = ""
        if move_id == "pollenpuff":
            pass
        if move_id in ("coaching",):
            a = self.ally(side, slot)
            if a:
                _boost(a[2], "atk", 1, s)
                _boost(a[2], "def", 1, s)
        if move_id == "decorate":
            pass
        if move_id == "bellydrum" and mon.hp * 2 > mon.maxhp:
            _chip(mon, mon.maxhp // 2)
            mon.boosts["atk"] = 6
        if move_id == "partingshot" or (md.get("selfSwitch") and self.rng is not None):
            if self.replace is not None and self.rng is not None and sd.bench() and mon.alive:
                c = self.replace(s, side, slot)
                if c is not None:
                    self.switch_in(side, slot, c)

    # ---- end of turn ----
    def end_of_turn(self) -> None:
        s = self.s
        f = s.field
        for side, slot, mon in list(s.actives()):
            w = f.weather
            if w == "sand" and not any(mon.has_type(t) for t in ("Rock", "Ground", "Steel")) and \
                    mon.ability not in ("sandveil", "sandrush", "sandforce", "overcoat", "magicguard") and \
                    mon.item != "safetygoggles":
                _chip(mon, mon.maxhp // 16)
            if f.terrain == "grassy" and mon.grounded(f):
                _heal(mon, mon.maxhp // 16)
            if mon.item == "leftovers":
                _heal(mon, mon.maxhp // 16)
            if mon.item == "blacksludge":
                if mon.has_type("Poison"):
                    _heal(mon, mon.maxhp // 16)
                else:
                    _chip(mon, mon.maxhp // 8)
            if mon.ability == "raindish" and w == "rain" or mon.ability == "icebody" and w == "snow":
                _heal(mon, mon.maxhp // 16)
            if mon.ability == "dryskin":
                if w == "rain":
                    _heal(mon, mon.maxhp // 8)
                elif w == "sun":
                    _chip(mon, mon.maxhp // 8)
            if mon.ability == "solarpower" and w == "sun":
                _chip(mon, mon.maxhp // 8)
            if mon.vol.get("leechseed"):
                ls, lsl = mon.vol["leechseed"]
                amount = min(mon.hp, mon.maxhp // 8)
                _chip(mon, amount)
                seeder = self.mon_at(ls, lsl)
                if seeder:
                    _heal(seeder, amount)
            if mon.vol.get("saltcure"):
                _chip(mon, mon.maxhp // (4 if (mon.has_type("Water") or mon.has_type("Steel")) else 8))
            if mon.status == "brn":
                _chip(mon, mon.maxhp // 16)
            elif mon.status == "psn":
                if mon.ability == "poisonheal":
                    _heal(mon, mon.maxhp // 8)
                else:
                    _chip(mon, mon.maxhp // 8)
            elif mon.status == "tox":
                mon.status_turns += 1
                if mon.ability == "poisonheal":
                    _heal(mon, mon.maxhp // 8)
                else:
                    _chip(mon, mon.maxhp * min(15, mon.status_turns) // 16)
            if mon.vol.get("yawn"):
                mon.vol["yawn"] -= 1
                if mon.vol["yawn"] <= 0:
                    mon.vol.pop("yawn")
                    if can_status(mon, "slp", s, side):
                        _set_status(mon, "slp", self.rng)
            if mon.item == "flameorb" and can_status(mon, "brn", s, side, by_foe=False):
                _set_status(mon, "brn", self.rng)
            if mon.item == "toxicorb" and can_status(mon, "tox", s, side, by_foe=False):
                _set_status(mon, "tox", self.rng)
            if mon.ability == "speedboost" and mon.turns_out > 0:
                _boost(mon, "spe", 1, s)
            for k in ("taunt", "encore"):
                if mon.vol.get(k):
                    mon.vol[k] -= 1
                    if mon.vol[k] <= 0:
                        mon.vol.pop(k)
                        mon.vol.pop("encoremove", None)
        for sd in s.sides:
            for attr in ("tailwind", "reflect", "lightscreen", "auroraveil", "safeguard"):
                if getattr(sd, attr):
                    setattr(sd, attr, getattr(sd, attr) - 1)
        if f.weather_turns:
            f.weather_turns -= 1
            if f.weather_turns == 0:
                f.weather = ""
        if f.terrain_turns:
            f.terrain_turns -= 1
            if f.terrain_turns == 0:
                f.terrain = ""
        if f.trickroom:
            f.trickroom -= 1
        if f.gravity:
            f.gravity -= 1
        for _, _, mon in s.actives():
            mon.turns_out += 1
            if mon.ability in ("protosynthesis", "quarkdrive") and mon.item == "boosterenergy" and not mon.vol.get("proto"):
                natural = (mon.ability == "protosynthesis" and f.weather == "sun") or \
                          (mon.ability == "quarkdrive" and f.terrain == "electric")
                if not natural:
                    mon.vol["proto"] = True
                    mon.item = ""
        # Replace fainted Pokémon
        if self.replace is not None:
            for side in (0, 1):
                sd = s.sides[side]
                for slot in (0, 1):
                    idx = sd.active[slot]
                    if idx is not None and not sd.mons[idx].alive:
                        sd.active[slot] = None
                for slot in (0, 1):
                    if sd.active[slot] is None and sd.bench():
                        c = self.replace(s, side, slot)
                        if c is not None:
                            sd.active[slot] = c
                            sd.mons[c].turns_out = 0
                            sd.mons[c].revealed = True
            for side, slot, _ in list(s.actives()):
                if s.sides[side].mons[s.sides[side].active[slot]].turns_out == 0:
                    self.on_entry(side, slot)


def resolve(state: State, actions: list[list[Action]], rng: random.Random | None = None,
            replace: Callable[[State, int, int], int | None] | None = None, copy: bool = True) -> State:
    s = state.copy() if copy else state
    return Resolver(s, rng, replace).run(actions)


def start_battle(state: State) -> None:
    """Entry abilities of both leads, faster first."""
    r = Resolver(state, None)
    order = sorted(state.actives(), key=lambda t: -effective_speed(t[2], state, t[0]))
    for side, slot, _ in order:
        r.on_entry(side, slot)


# ---- legal options (offline play; the platform gives us its own list) ----

def slot_options(state: State, side: int, slot: int, taken_switch: int | None = None) -> list[Action]:
    sd = state.sides[side]
    mon = sd.active_mon(slot)
    out: list[Action] = []
    if mon is None:
        return [("pass",)]
    moves = mon.build.moves
    if mon.choice_lock and mon.choice_lock in moves:
        moves = [mon.choice_lock]
    if mon.vol.get("encore") and mon.vol.get("encoremove") in moves:
        moves = [mon.vol["encoremove"]]
    for mid in moves:
        md = move_data(mid)
        if md["category"] == "Status" and (mon.vol.get("taunt") or mon.item == "assaultvest"):
            continue
        if mid in FIRST_TURN_ONLY and mon.turns_out > 0:
            continue
        kind = md.get("target", "normal")
        if kind in ("normal", "any", "adjacentFoe"):
            for t in (1, 2):
                if state.sides[1 - side].active_mon(t - 1) is not None:
                    out.append(("move", mid, t))
            ally = sd.active_mon(1 - slot)
            if ally is not None and (md["category"] == "Status" or mid in ("pollenpuff",)):
                out.append(("move", mid, -(2 - slot)))
            if not any(o[1] == mid for o in out if o[0] == "move"):
                out.append(("move", mid, 1 if state.sides[1 - side].active_mon(0) else 2))
        elif kind == "adjacentAllyOrSelf":
            out.append(("move", mid, -(slot + 1)))
        elif kind == "adjacentAlly":
            if sd.active_mon(1 - slot) is not None:
                out.append(("move", mid, -(2 - slot)))
        else:
            out.append(("move", mid, 0))
    for i in sd.bench():
        if i != taken_switch:
            out.append(("switch", i))
    return out or [("pass",)]
