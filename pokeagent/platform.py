"""Translate between the platform's JSON (docs/pokemon-skill.md) and the engine's State/Actions.

Parsing is deliberately tolerant: the guide documents the field names but not every value's
spelling (the battle board appears to be serialized from poke-env objects, so enums may arrive
as "SUNNYDAY", "Weather.SUNNYDAY" or "sunnyday"). Anything unreadable falls back to a neutral
default rather than raising, because a crash here costs a whole turn.
"""

from __future__ import annotations

from typing import Any

from .dex import to_id
from .engine import Action
from .model import BOOSTABLE, Build, Field, Mon, Side, State

WEATHER_NAMES = {"sunnyday": "sun", "sun": "sun", "desolateland": "sun", "harshsunshine": "sun",
                 "raindance": "rain", "rain": "rain", "primordialsea": "rain", "heavyrain": "rain",
                 "sandstorm": "sand", "sand": "sand", "snow": "snow", "snowscape": "snow", "hail": "snow"}
TERRAIN_NAMES = {"electricterrain": "electric", "grassyterrain": "grassy", "psychicterrain": "psychic",
                 "mistyterrain": "misty"}
STATUS_NAMES = {"brn": "brn", "par": "par", "psn": "psn", "tox": "tox", "slp": "slp", "frz": "frz", "fnt": "fnt"}


def _enum_id(value: Any) -> str:
    """'Weather.SUNNYDAY' / 'SUNNYDAY' / 'Sunny Day' -> 'sunnyday'."""
    text = str(value or "")
    if "." in text:
        text = text.split(".")[-1]
    return to_id(text)


def _keys_with_values(raw: Any) -> list[tuple[str, Any]]:
    if isinstance(raw, dict):
        return [(_enum_id(k), v) for k, v in raw.items()]
    if isinstance(raw, (list, tuple)):
        return [(_enum_id(x if not isinstance(x, dict) else x.get("name") or x.get("id")), None) for x in raw]
    if raw:
        return [(_enum_id(raw), None)]
    return []


def _remaining(value: Any, turn: int, duration: int) -> int:
    """poke-env stores the turn an effect started; convert to turns left (best effort)."""
    if isinstance(value, dict):
        for k in ("turns_left", "remaining", "turns"):
            if isinstance(value.get(k), int):
                return max(1, value[k])
        value = value.get("start_turn") or value.get("turn")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return duration
    v = int(value)
    if 0 < v <= turn:
        return max(1, duration - (turn - v))
    if 0 < v <= duration:
        return v  # looks like a turns-left counter
    return duration


def parse_field(obs: dict, turn: int) -> Field:
    f = Field()
    for key, val in _keys_with_values(obs.get("weather")):
        if key in WEATHER_NAMES:
            f.weather = WEATHER_NAMES[key]
            f.weather_turns = _remaining(val, turn, 5)
    for key, val in _keys_with_values(obs.get("fields")):
        if key in TERRAIN_NAMES:
            f.terrain = TERRAIN_NAMES[key]
            f.terrain_turns = _remaining(val, turn, 5)
        elif key == "trickroom":
            f.trickroom = _remaining(val, turn, 5)
        elif key == "gravity":
            f.gravity = _remaining(val, turn, 5)
    return f


def apply_side_conditions(side: Side, raw: Any, turn: int) -> None:
    for key, val in _keys_with_values(raw):
        if key == "tailwind":
            side.tailwind = _remaining(val, turn, 4)
        elif key == "reflect":
            side.reflect = _remaining(val, turn, 5)
        elif key == "lightscreen":
            side.lightscreen = _remaining(val, turn, 5)
        elif key == "auroraveil":
            side.auroraveil = _remaining(val, turn, 5)
        elif key == "safeguard":
            side.safeguard = _remaining(val, turn, 5)


def card_build(card: dict) -> Build | None:
    try:
        return Build.from_card(card)
    except Exception:
        return None


def summary_build(summary: dict) -> Build:
    """Fallback when we don't have the card: build from what the observation shows."""
    card = {
        "card_id": summary.get("species"),
        "species": summary.get("species") or summary.get("name"),
        "item": summary.get("item") or "",
        "ability": summary.get("ability") or "",
        "nature": "Serious",
        "level": summary.get("level") or 50,
        "evs": {"hp": 84, "atk": 84, "def": 84, "spa": 84, "spd": 84, "spe": 84},
        "moves": list((summary.get("moves") or {}).keys()) if isinstance(summary.get("moves"), dict)
        else list(summary.get("moves") or []),
    }
    return Build.from_card(card)


def _status(summary: dict) -> str:
    st = _enum_id(summary.get("status"))
    return STATUS_NAMES.get(st, "")


def _boosts(summary: dict) -> dict:
    raw = summary.get("boosts") or {}
    out = {b: 0 for b in BOOSTABLE}
    for k, v in raw.items():
        k = {"attack": "atk", "defense": "def", "specialattack": "spa", "specialdefense": "spd",
             "speed": "spe", "evasiveness": "evasion"}.get(to_id(k), to_id(k))
        if k in out and isinstance(v, (int, float)):
            out[k] = int(v)
    return out


def make_mon(build: Build, summary: dict | None, opponent: bool) -> Mon:
    mon = Mon.fresh(build)
    if not summary:
        return mon
    frac = summary.get("current_hp_fraction")
    cur, mx = summary.get("current_hp"), summary.get("max_hp")
    if not opponent and isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0 and mx != 100:
        mon.maxhp = int(mx)
        mon.hp = int(cur)
    elif isinstance(frac, (int, float)):
        mon.hp = int(round(mon.maxhp * float(frac)))
    elif isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
        mon.hp = int(round(mon.maxhp * cur / mx))
    st = _status(summary)
    if st == "fnt" or summary.get("fainted") or mon.hp <= 0:
        mon.hp, mon.fainted = 0, True
    else:
        mon.status = st
    mon.boosts = _boosts(summary)
    item = summary.get("item")
    if isinstance(item, str) and item not in ("unknown_item", "unknownitem"):
        mon.item = to_id(item)  # "" means it was used up or knocked off
    elif item is None and summary.get("item_known"):
        mon.item = ""
    ab = to_id(summary.get("ability"))
    if ab:
        mon.ability = ab
    mon.revealed = bool(summary.get("revealed", True))
    if summary.get("first_turn") is False or summary.get("turns_out"):
        mon.turns_out = int(summary.get("turns_out") or 1)
    return mon


def _team_entries(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        return [v for v in raw.values() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, dict)]
    return []


def _species_key(summary: dict | None) -> str:
    if not summary:
        return ""
    return to_id(summary.get("species") or summary.get("name"))


def _match_build(key: str, builds: dict[str, Build]) -> Build | None:
    if key in builds:
        return builds[key]
    for sid, b in builds.items():
        if sid.startswith(key) or key.startswith(sid):
            return b
    return None


class BoardView:
    """The engine State for one decision plus the mapping back to platform choices."""

    def __init__(self, state: State, my_species: list[str], opp_species: list[str]):
        self.state = state
        self.my_species = my_species   # index -> species id, side 0
        self.opp_species = opp_species


def parse_board(obs: dict, my_builds: dict[str, Build], opp_builds: dict[str, Build],
                template: dict | None = None, opp_brought: int = 4) -> BoardView:
    """Build a State (side 0 = us) from a doubles battle observation."""
    turn = int(obs.get("turn") or 1)
    field = parse_field(obs, turn)

    def build_side(team_raw, actives_raw, builds, opponent):
        entries = _team_entries(team_raw)
        actives = actives_raw if isinstance(actives_raw, list) else [actives_raw]
        actives = (list(actives) + [None, None])[:2]
        mons, keys = [], []
        for e in entries:
            key = _species_key(e)
            if not key or key in keys:
                continue
            b = _match_build(key, builds) or summary_build(e)
            mons.append(make_mon(b, e, opponent))
            keys.append(key)
        for a in actives:
            key = _species_key(a)
            if key and key not in keys:
                b = _match_build(key, builds) or summary_build(a)
                mons.append(make_mon(b, a, opponent))
                keys.append(key)
        active_idx: list[int | None] = []
        for a in actives:
            key = _species_key(a)
            if key and key in keys:
                idx = keys.index(key)
                # the active entry is the freshest summary
                fresh = make_mon(mons[idx].build, a, opponent)
                fresh.turns_out = mons[idx].turns_out if mons[idx].turns_out else fresh.turns_out
                mons[idx] = fresh
                active_idx.append(idx)
            else:
                active_idx.append(None)
        return mons, keys, active_idx

    my_mons, my_keys, my_active = build_side(obs.get("team"), obs.get("active_pokemon"), my_builds, False)
    opp_team = obs.get("opponent_team")
    opp_mons, opp_keys, opp_active = build_side(opp_team, obs.get("opponent_active_pokemon"), opp_builds, True)

    # Use the template's named targets to confirm which opponent sits in position A (1) and B (2)
    if template:
        pos = {}
        for slot in template.get("slots") or []:
            for opt in slot.get("options") or []:
                for t in opt.get("target_options") or []:
                    if t.get("side") == "opponent" and t.get("target") in (1, 2) and t.get("species"):
                        pos[t["target"] - 1] = to_id(t["species"])
        for p, key in pos.items():
            idx = next((i for i, k in enumerate(opp_keys) if k == key or k.startswith(key) or key.startswith(k)), None)
            if idx is None and key in opp_builds:
                opp_mons.append(Mon.fresh(opp_builds[key]))
                opp_keys.append(key)
                idx = len(opp_keys) - 1
            if idx is not None:
                other = opp_active[1 - p]
                if other == idx:
                    opp_active[1 - p] = opp_active[p]
                opp_active[p] = idx

    # Opponent Pokémon not yet seen: mark the ones only known from preview as unrevealed
    revealed = [m for i, m in enumerate(opp_mons) if m.revealed or i in opp_active or m.fainted or m.hp_frac < 1]
    hidden = [m for m in opp_mons if m not in revealed]
    for m in hidden:
        m.vol["not_brought"] = True  # unknown whether brought; counted through `unseen` instead
    unseen = max(0, opp_brought - len(revealed))

    my_side = Side(mons=my_mons, active=my_active)
    opp_side = Side(mons=opp_mons, active=opp_active, unseen=unseen)
    apply_side_conditions(my_side, obs.get("side_conditions"), turn)
    apply_side_conditions(opp_side, obs.get("opponent_side_conditions"), turn)
    state = State(sides=[my_side, opp_side], field=field, turn=turn)
    return BoardView(state, my_keys, opp_keys)


def template_options(view: BoardView, template: dict) -> tuple[list[list[Action]], dict]:
    """Per-slot engine Actions from the doubles_turn template, and how to send each back."""
    slots = sorted(template.get("slots") or [], key=lambda s: s.get("slot", 0))
    per_slot: list[list[Action]] = [[("pass",)], [("pass",)]]
    back: dict = {}
    for slot in slots:
        n = slot.get("slot", 0)
        if n not in (0, 1):
            continue
        acts: list[Action] = []
        for opt in slot.get("options") or []:
            kind = opt.get("type")
            if kind == "pass":
                a = ("pass",)
                back[(n, a)] = {"type": "pass"}
                acts.append(a)
            elif kind == "switch":
                key = to_id(opt.get("species"))
                idx = next((i for i, k in enumerate(view.my_species) if k == key or k.startswith(key) or key.startswith(k)), None)
                if idx is None:
                    continue
                a = ("switch", idx)
                back[(n, a)] = {"type": "switch", "species": opt.get("species")}
                acts.append(a)
            elif kind == "move":
                mid = to_id(opt.get("move_id"))
                targets = [t for t in (opt.get("targets") or []) if isinstance(t, int) and not isinstance(t, bool)]
                if not targets:
                    a = ("move", mid, 0)
                    back[(n, a)] = {"type": "move", "move_id": opt.get("move_id")}
                    acts.append(a)
                    continue
                for t in targets:
                    if t < 0 and t == -(n + 1) and len(targets) > 1:
                        pass  # self-target: keep, the engine treats it as "self"
                    a = ("move", mid, t)
                    back[(n, a)] = {"type": "move", "move_id": opt.get("move_id"), "target": t}
                    acts.append(a)
        per_slot[n] = acts or [("pass",)]
    return per_slot, back


def prune_ally_hits(per_slot: list[list[Action]]) -> list[list[Action]]:
    """Drop attacks aimed at our own Pokémon unless nothing else is left (the search can still
    choose healing/support moves on the ally, which are status moves or Pollen Puff)."""
    from .engine import move_data
    out = []
    for n, acts in enumerate(per_slot):
        keep = []
        for a in acts:
            if a[0] == "move" and a[2] < 0 and move_data(a[1])["category"] != "Status" and a[1] != "pollenpuff":
                continue
            keep.append(a)
        out.append(keep or acts)
    return out


def to_platform(choice: tuple[Action, Action], back: dict) -> dict:
    out = {"type": "doubles_turn"}
    for n in (0, 1):
        out[f"slot_{n}"] = back.get((n, choice[n]), {"type": "pass"})
    return out
