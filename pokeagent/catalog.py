"""The static card catalog (pokeagent/data/cards.json): exact sets of cards seen in live drafts,
frozen before the deadline and never written during play (organizer-approved static knowledge).

Used only when a card wasn't visible in the current match; cards on offer this match always win.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .dex import to_id

_PATH = Path(__file__).resolve().parent / "data" / "cards.json"


@lru_cache(maxsize=1)
def _cards() -> tuple[dict, dict]:
    try:
        cards = json.loads(_PATH.read_text())
    except Exception:
        return {}, {}
    return {c["card_id"]: c for c in cards}, {to_id(c["species"]): c for c in cards}


def lookup(card_id: str | None = None, species: str | None = None) -> dict | None:
    by_id, by_species = _cards()
    if card_id and card_id in by_id:
        return dict(by_id[card_id])
    key = to_id(species)
    if key and key in by_species:
        return dict(by_species[key])
    return None
