"""Draft picks: score every card on offer for our team, against theirs, and for denial."""

from __future__ import annotations

from . import dex
from .model import Build
from .roles import duel, prior, register, roles_of

ATTACK_TYPES = dex.TYPES


def weak_to(build: Build, atk_type: str) -> bool:
    eff = dex.type_eff(atk_type, build.types)
    if build.ability in ("levitate", "eartheater") and atk_type == "Ground":
        return False
    if build.ability in ("flashfire", "wellbakedbody") and atk_type == "Fire":
        return False
    if build.ability in ("waterabsorb", "stormdrain", "dryskin") and atk_type == "Water":
        return False
    if build.ability in ("voltabsorb", "lightningrod", "motordrive") and atk_type == "Electric":
        return False
    if build.ability == "sapsipper" and atk_type == "Grass":
        return False
    if build.item == "airballoon" and atk_type == "Ground":
        return False
    return eff > 1


def general_strength(key: str, pool_keys: list[str]) -> float:
    others = [k for k in pool_keys if k != key]
    if not others:
        return 0.0
    return sum(duel(key, o) for o in others) / len(others)


def synergy(key: str, team: list[str]) -> float:
    """What adding `key` does for a team (its roles, combos and shared weaknesses)."""
    r = roles_of(key)
    rs = [roles_of(k) for k in team]
    b = _build(key)
    s = 0.0
    if r.fake_out and not any(x.fake_out for x in rs):
        s += 1.4
    if r.intimidate and not any(x.intimidate for x in rs):
        s += 1.2
    if r.speed_control and not any(x.speed_control for x in rs):
        s += 1.4
    if r.redirect and not any(x.redirect for x in rs):
        s += 0.8
    if r.spread:
        s += 0.4 * max(0, 2 - sum(1 for x in rs if x.spread))
    # Too many supports: we bring only 4 and need damage
    n_support = sum(1 for x in rs if x.support >= 2)
    if r.support >= 2 and n_support >= 2:
        s -= 0.8 * (n_support - 1)
    # Trick Room
    setters = sum(1 for x in rs if x.trick_room)
    slow_hitters = sum(1 for k, x in zip(team, rs) if x.speed <= 70 and not x.trick_room and _offense(k) >= 1)
    if r.trick_room:
        s += 0.6 * min(slow_hitters, 3) if setters == 0 else 0.2
    elif setters and r.speed <= 70 and _offense(key) >= 1:
        s += 0.7
    elif setters and r.speed >= 130 and not any(x.tailwind for x in rs):
        s -= 0.3
    # Tailwind
    if r.tailwind and not any(x.tailwind for x in rs):
        s += 0.3 * sum(1 for x in rs if 70 < x.speed <= 120)
    elif any(x.tailwind for x in rs) and 70 < r.speed <= 120 and _offense(key) >= 1:
        s += 0.3
    # Weather and terrain combos
    if r.weather:
        s += 1.0 * sum(1 for x in rs if x.weather_abuser == r.weather) + 0.3 * sum(
            1 for x in rs if x.weather_abuser == r.weather + "?")
        if any(x.weather and x.weather != r.weather for x in rs):
            s -= 0.8
    if r.weather_abuser:
        base = r.weather_abuser.rstrip("?")
        if any(x.weather == base for x in rs):
            s += 1.0 if not r.weather_abuser.endswith("?") else 0.3
        elif not r.weather_abuser.endswith("?") and b.ability in ("chlorophyll", "swiftswim", "sandrush", "slushrush"):
            s -= 0.5  # a weather abuser with no weather is weak
    if r.terrain:
        s += 0.3 * sum(1 for k in team if _terrain_user(k, r.terrain))
    elif any(roles_of(k).terrain and _terrain_user(key, roles_of(k).terrain) for k in team):
        s += 0.3
    # Shared weaknesses
    for t in ATTACK_TYPES:
        n = sum(1 for k in team if weak_to(_build(k), t))
        if weak_to(b, t) and n >= 2:
            s -= 0.5 * (n - 1)
    # Attacking balance
    phys = sum(1 for x in rs if x.physical)
    spec = sum(1 for x in rs if x.special)
    if r.special and not r.physical and spec >= phys + 2:
        s -= 0.3
    if r.physical and not r.special and phys >= spec + 2:
        s -= 0.3
    return s


def _offense(key: str) -> int:
    b = _build(key)
    return 1 if max(b.stats["atk"], b.stats["spa"]) >= 110 else 0


def _terrain_user(key: str, terrain: str) -> bool:
    b = _build(key)
    if terrain == "psychic":
        return "expandingforce" in b.moves
    if terrain == "electric":
        return b.ability in ("quarkdrive", "surgesurfer") or "risingvoltage" in b.moves
    if terrain == "grassy":
        return "grassyglide" in b.moves or b.ability == "grasspelt"
    return False


def _build(key: str) -> Build:
    from .roles import _BUILDS
    return _BUILDS[key]


def score_card(key: str, mine: list[str], theirs: list[str], pool: list[str], picks_left_mine: int) -> float:
    strength = 0.35 * prior(_build(key)) + 3.0 * general_strength(key, pool)
    fit = synergy(key, mine)
    vs_them = 2.0 * (sum(duel(key, t) for t in theirs) / len(theirs)) if theirs else 0.0
    # Denial: how much the opponent would want this card
    their_want = 0.35 * prior(_build(key)) + 3.0 * general_strength(key, pool) + synergy(key, theirs)
    their_want += 2.0 * (sum(duel(key, m) for m in mine) / len(mine)) if mine else 0.0
    denial_w = 0.35 if picks_left_mine > 1 else 0.15
    # Late picks matter less (we bring 4 of 6), so lean on fit and coverage
    if len(mine) >= 4:
        fit *= 1.2
        strength *= 0.8
    return strength + fit + vs_them + denial_w * their_want


def choose_pick(available: list[dict], my_cards: list[dict], their_cards: list[dict],
                pool_cards: list[dict], picks_left_mine: int = 6) -> tuple[str, str]:
    """Return (card_id, short public reason)."""
    keys = {}
    for c in available + my_cards + their_cards + pool_cards:
        try:
            b = Build.from_card(c)
        except Exception:
            continue
        keys[c["card_id"]] = register(b)
    avail = [c for c in available if c["card_id"] in keys]
    if not avail:
        return available[0]["card_id"], "Taking the best remaining card."
    mine = [keys[c["card_id"]] for c in my_cards if c["card_id"] in keys]
    theirs = [keys[c["card_id"]] for c in their_cards if c["card_id"] in keys]
    pool = list(dict.fromkeys(keys[c["card_id"]] for c in pool_cards + available + my_cards + their_cards
                              if c["card_id"] in keys))
    scored = sorted(((score_card(keys[c["card_id"]], mine, theirs, pool, picks_left_mine), c) for c in avail),
                    key=lambda t: -t[0])
    best = scored[0][1]
    return best["card_id"], _reason(keys[best["card_id"]], mine, theirs)


def _reason(key: str, mine: list[str], theirs: list[str]) -> str:
    r = roles_of(key)
    b = _build(key)
    bits = []
    if r.fake_out:
        bits.append("Fake Out pressure")
    if r.intimidate:
        bits.append("Intimidate")
    if r.tailwind:
        bits.append("Tailwind")
    if r.trick_room:
        bits.append("Trick Room")
    if r.redirect:
        bits.append("redirection")
    if r.spread:
        bits.append("spread damage")
    if theirs:
        beats = [t for t in theirs if duel(key, t) > 0.3]
        if beats:
            bits.append("beats " + ", ".join(_build(t).name for t in beats[:2]))
    if not bits:
        bits.append("strong attacker")
    return f"{b.name}: " + ", ".join(bits[:3]) + "."
