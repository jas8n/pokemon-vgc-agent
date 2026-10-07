"""Showdown's Gen 9 data (Pokédex, moves, type chart, natures), via poke-env's static files."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import poke_env

_STATIC = Path(poke_env.__file__).parent / "data" / "static"


def to_id(name: str | None) -> str:
    """Showdown id: lowercase, letters and digits only ("Urshifu-Rapid-Strike" -> "urshifurapidstrike")."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _load(path: str) -> dict:
    return json.loads((_STATIC / path).read_text())


POKEDEX: dict = _load("pokedex/gen9pokedex.json")
MOVES: dict = _load("moves/gen9moves.json")
_TYPECHART: dict = _load("typechart/gen9typechart.json")
NATURES: dict = {to_id(k): v for k, v in _load("natures.json").items()}

TYPES = [t.capitalize() for t in _TYPECHART]
_TAKEN = {t.capitalize(): v["damageTaken"] for t, v in _TYPECHART.items()}
_EFF_CODE = {0: 1.0, 1: 2.0, 2: 0.5, 3: 0.0}


def norm_type(t: str | None) -> str:
    return (t or "").strip().capitalize()


@lru_cache(maxsize=None)
def type_eff(atk_type: str, def_types: tuple[str, ...]) -> float:
    """Multiplier of an attacking type against a defender's types (no abilities)."""
    mult = 1.0
    for d in def_types:
        code = _TAKEN.get(norm_type(d), {}).get(norm_type(atk_type), 0)
        mult *= _EFF_CODE.get(code, 1.0)
    return mult


def species(name: str) -> dict:
    sid = to_id(name)
    if sid in POKEDEX:
        return POKEDEX[sid]
    # Platform ids sometimes drop the forme ("urshifu" for Urshifu-Single-Strike, etc.)
    for key, entry in POKEDEX.items():
        if to_id(entry.get("name")) == sid:
            return entry
    raise KeyError(f"unknown species {name!r}")


def move(name: str) -> dict:
    mid = to_id(name)
    if mid.startswith("hiddenpower"):
        mid = "hiddenpower"
    return MOVES[mid]


def nature_mult(nature: str | None, stat: str) -> float:
    entry = NATURES.get(to_id(nature or "serious"), {})
    return float(entry.get(stat, 1.0))
