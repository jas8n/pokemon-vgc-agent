"""Read Showdown's battle log (the observation's `protocol_log`) for what the board summary
doesn't say: how many turns Tailwind / Trick Room / screens / terrain have left, Protect streaks,
when each Pokémon came in (Fake Out), choice locks, and items revealed or used up.

The log is the authoritative, player-visible record of the match, so this only reads our own
seat's view (Official Rules section 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dex import to_id
from .model import State

PROTECT_NAMES = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap",
                 "burningbulwark", "obstruct", "maxguard"}
SIDE_TURNS = {"tailwind": 4, "reflect": 5, "lightscreen": 5, "auroraveil": 5, "safeguard": 5}
FIELD_TURNS = {"trickroom": 5, "gravity": 5, "electricterrain": 5, "grassyterrain": 5,
               "psychicterrain": 5, "mistyterrain": 5}
WEATHERS = {"sunnyday": "sun", "desolateland": "sun", "raindance": "rain", "primordialsea": "rain",
            "sandstorm": "sand", "snow": "snow", "snowscape": "snow", "hail": "snow"}
TERRAINS = {"electricterrain": "electric", "grassyterrain": "grassy", "psychicterrain": "psychic",
            "mistyterrain": "misty"}


@dataclass
class LogFacts:
    turn: int = 0
    side_start: dict = field(default_factory=dict)   # (player, cond) -> (turn started, holder item)
    field_start: dict = field(default_factory=dict)  # cond -> turn started
    weather: str | None = None                       # None = log said nothing
    weather_start: int = 0
    came_in: dict = field(default_factory=dict)      # (player, species) -> turn it came in
    protect_turns: dict = field(default_factory=dict)  # (player, species) -> list of turns it protected
    last_move: dict = field(default_factory=dict)    # (player, species) -> move id
    moves_used: dict = field(default_factory=dict)   # (player, species) -> set of move ids
    items: dict = field(default_factory=dict)        # (player, species) -> item id ("" = gone)
    proto: dict = field(default_factory=dict)        # (player, species) -> Protosynthesis/Quark Drive active
    slept: dict = field(default_factory=dict)        # (player, species) -> turns it has stayed asleep so far


def _who(token: str) -> tuple[str, str]:
    """'p2a: Gholdengo' -> ('p2', 'gholdengo')"""
    pos, _, name = token.partition(":")
    return pos.strip()[:2], to_id(name)


def _species_from_details(details: str) -> str:
    return to_id(details.split(",")[0])


def read_log(lines) -> LogFacts:
    f = LogFacts()
    nick_to_species: dict[tuple[str, str], str] = {}
    if isinstance(lines, str):
        lines = lines.splitlines()
    for raw in lines or []:
        if not isinstance(raw, str) or not raw.startswith("|"):
            continue
        parts = raw.split("|")[1:]
        if not parts:
            continue
        tag = parts[0]
        if tag == "turn" and len(parts) > 1:
            try:
                f.turn = int(parts[1])
            except ValueError:
                pass
        elif tag in ("switch", "drag", "replace") and len(parts) > 2:
            player, nick = _who(parts[1])
            sp = _species_from_details(parts[2])
            nick_to_species[(player, nick)] = sp
            f.came_in[(player, sp)] = f.turn + 1 if f.turn else 1  # first turn it can act
            f.protect_turns.pop((player, sp), None)
            f.last_move.pop((player, sp), None)
            f.proto.pop((player, sp), None)  # a Booster Energy boost ends on switching out
        else:
            def species_of(token):
                player, nick = _who(token)
                return player, nick_to_species.get((player, nick), nick)

            if tag == "move" and len(parts) > 2:
                player, sp = species_of(parts[1])
                mid = to_id(parts[2])
                f.last_move[(player, sp)] = mid
                f.moves_used.setdefault((player, sp), set()).add(mid)
            elif tag == "-singleturn" and len(parts) > 2:
                player, sp = species_of(parts[1])
                eff = to_id(parts[2].replace("move:", ""))
                if eff in PROTECT_NAMES:
                    f.protect_turns.setdefault((player, sp), []).append(f.turn)
            elif tag in ("-activate", "-start") and len(parts) > 2 and (
                    "protosynthesis" in to_id(parts[2]) or "quarkdrive" in to_id(parts[2])):
                player, sp = species_of(parts[1])
                f.proto[(player, sp)] = True
            elif tag == "-end" and len(parts) > 2 and (
                    "protosynthesis" in to_id(parts[2]) or "quarkdrive" in to_id(parts[2])):
                player, sp = species_of(parts[1])
                f.proto[(player, sp)] = False
            elif tag == "-sidestart" and len(parts) > 2:
                player = parts[1].split(":")[0].strip()[:2]
                cond = to_id(parts[2].replace("move:", ""))
                f.side_start[(player, cond)] = f.turn
            elif tag == "-sideend" and len(parts) > 2:
                player = parts[1].split(":")[0].strip()[:2]
                cond = to_id(parts[2].replace("move:", ""))
                f.side_start.pop((player, cond), None)
            elif tag == "-fieldstart" and len(parts) > 1:
                cond = to_id(parts[1].replace("move:", ""))
                if cond in TERRAINS:
                    for t in TERRAINS:
                        f.field_start.pop(t, None)
                f.field_start[cond] = f.turn
            elif tag == "-fieldend" and len(parts) > 1:
                f.field_start.pop(to_id(parts[1].replace("move:", "")), None)
            elif tag == "-weather" and len(parts) > 1:
                w = to_id(parts[1])
                upkeep = any(p.strip() == "[upkeep]" for p in parts[2:])
                if w == "none":
                    f.weather, f.weather_start = "", 0
                elif not upkeep:
                    f.weather, f.weather_start = WEATHERS.get(w, ""), f.turn
            elif tag == "-status" and len(parts) > 2 and to_id(parts[2]) == "slp":
                player, sp = species_of(parts[1])
                f.slept[(player, sp)] = 0
            elif tag == "cant" and len(parts) > 2 and to_id(parts[2]) == "slp":
                player, sp = species_of(parts[1])
                f.slept[(player, sp)] = f.slept.get((player, sp), 0) + 1
            elif tag == "-curestatus" and len(parts) > 2 and to_id(parts[2]) == "slp":
                player, sp = species_of(parts[1])
                f.slept.pop((player, sp), None)
            elif tag == "-item" and len(parts) > 2:
                player, sp = species_of(parts[1])
                f.items[(player, sp)] = to_id(parts[2])
            elif tag == "-enditem" and len(parts) > 2:
                player, sp = species_of(parts[1])
                f.items[(player, sp)] = ""
    return f


def _remaining(start_turn: int, now: int, duration: int) -> int:
    """Turns left for an effect set during `start_turn`, as of the start of turn `now`.
    The log's turn counter is the turn being played when the effect started (0 = before turn 1)."""
    used = now - max(start_turn, 1)
    return max(1, duration - used)


def apply_log(state: State, facts: LogFacts, me: str, mon_items: dict | None = None) -> None:
    """Overwrite the parts of `state` (side 0 = us) the log knows better."""
    now = max(facts.turn, state.turn)
    players = {me: 0, ("p1" if me == "p2" else "p2"): 1}
    # Side conditions
    for player, side_idx in players.items():
        sd = state.sides[side_idx]
        for cond, dur in SIDE_TURNS.items():
            start = facts.side_start.get((player, cond))
            if start is None:
                continue
            if cond in ("reflect", "lightscreen", "auroraveil"):
                if any(m.build.item == "lightclay" for m in sd.mons):
                    dur = 8
            setattr(sd, cond, _remaining(start, now, dur))
    # Field
    tr = facts.field_start.get("trickroom")
    if tr is not None:
        state.field.trickroom = _remaining(tr, now, 5)
    for cond, terr in TERRAINS.items():
        start = facts.field_start.get(cond)
        if start is not None:
            ext = any(m.build.item == "terrainextender" for sd in state.sides for m in sd.mons)
            state.field.terrain = terr
            state.field.terrain_turns = _remaining(start, now, 8 if ext else 5)
    if facts.weather is not None:
        state.field.weather = facts.weather
        if facts.weather:
            state.field.weather_turns = _remaining(facts.weather_start, now, 5)
        else:
            state.field.weather_turns = 0
    # Per-Pokémon facts
    for player, side_idx in players.items():
        for mon in state.sides[side_idx].mons:
            key = (player, mon.sid)
            if key in facts.came_in:
                mon.turns_out = max(0, now - facts.came_in[key])
            turns = facts.protect_turns.get(key, [])
            streak = 0
            t = now - 1
            while t in turns:
                streak += 1
                t -= 1
            mon.protect_streak = streak
            if mon.status == "slp":
                # Sleep lasts 1-3 turns in Gen 9 and the board gives no counter: two more turns if it just
                # fell asleep, otherwise one (each turn spent asleep uses one up)
                mon.status_turns = max(1, 2 - facts.slept.get(key, 0))
            if key in facts.items:
                mon.item = facts.items[key]
            if facts.proto.get(key):
                mon.vol["proto"] = True
            elif facts.proto.get(key) is False:
                mon.vol.pop("proto", None)
            last = facts.last_move.get(key)
            if last:
                mon.vol["lastmove"] = last
                if side_idx == 1 and (mon.item.startswith("choice") or mon.build.ability == "gorillatactics") \
                        and last in mon.build.moves and mon.turns_out > 0:
                    mon.choice_lock = last


def my_player(obs: dict) -> str:
    """'p1' or 'p2' from the keys of our own team ('p2: Dragonite')."""
    team = obs.get("team")
    if isinstance(team, dict):
        for k in team:
            if isinstance(k, str) and k[:2] in ("p1", "p2"):
                return k[:2]
    return "p1"
