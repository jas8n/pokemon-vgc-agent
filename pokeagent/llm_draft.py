"""Optional LLM draft advisor: Claude chooses among the draft scorer's top few cards.

The scorer (pokeagent.draft.rank_picks) still decides which cards are worth considering; Claude only
breaks the choice between them, using its knowledge of the Reg I doubles meta. Any failure (no API
key, no SDK, timeout, an answer that is not one of the candidates) returns None and the agent keeps
the scorer's pick, so the 15 s draft clock is never at risk.

Needs ANTHROPIC_API_KEY in the environment or in the repo's .env. DRAFT_LLM_MODEL picks the model.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

MODEL = os.environ.get("DRAFT_LLM_MODEL", "claude-opus-5-5")
# The draft clock is 15 s per pick; the scorer takes ~1 s, so Claude gets 9 s (it thinks before answering)
TIMEOUT_S = 9.0
# Low effort keeps Opus at 1-5 s per pick (default effort often ran past 9 s)
EFFORT = "low"
CANDIDATES = 4

_client = None
_disabled = False
STATS: Counter = Counter()  # answered / changed / failed / no_answer, for offline reports


def _get_client():
    global _client, _disabled
    if _client is not None or _disabled:
        return _client
    try:
        import anthropic
        if not os.environ.get("ANTHROPIC_API_KEY"):
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            _disabled = True
            return None
        _client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=0)
    except Exception:
        _disabled = True
    return _client


def _describe(card: dict) -> str:
    moves = ", ".join(card.get("moves") or [])
    return (f"{card.get('species')} @ {card.get('item') or 'no item'} | {card.get('ability') or '?'} | "
            f"{card.get('nature') or '?'} | {moves}")


def _prompt(candidates: list[tuple[float, dict]], mine: list[dict], theirs: list[dict],
            others: list[dict], picks_left: int) -> str:
    def block(cards):
        return "\n".join(f"- {_describe(c)}" for c in cards) or "- (none yet)"
    cand = "\n".join(f"{i + 1}. [{c['card_id']}] {_describe(c)} (scorer value {s:.1f})"
                     for i, (s, c) in enumerate(candidates))
    return f"""You are drafting for a Pokémon VGC doubles battle: Gen 9, Regulation I rules, level 50, \
no Terastallization. Two players snake-draft 6 cards each from a random pool (Item Clause: no two \
cards on a team share an item). At Team Preview each player brings 4 of their 6 and leads 2.

Our team so far:
{block(mine)}

Opponent's team so far:
{block(theirs)}

Still in the pool besides the candidates:
{block(others)}

We have {picks_left} picks left. Our scorer shortlisted these candidates (higher value = scorer likes it more):
{cand}

Pick the candidate that most improves our chance of winning: think about the likely final teams, \
speed control, Fake Out / redirection / Intimidate support, type coverage, what the opponent is \
building toward, and denying them key cards. Answer with only the candidate's id in square brackets, \
e.g. [{candidates[0][1]['card_id']}]."""


def advise(ranked: list[tuple[float, dict]], mine: list[dict], theirs: list[dict],
           available: list[dict], picks_left: int) -> str | None:
    """card_id chosen by Claude from the top candidates, or None to keep the scorer's pick."""
    if len(ranked) < 2:
        return None
    client = _get_client()
    if client is None:
        return None
    candidates = ranked[:CANDIDATES]
    ids = {c["card_id"] for _, c in candidates}
    others = [c for c in available if c["card_id"] not in ids]
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=3000, extra_body={"output_config": {"effort": EFFORT}},
            messages=[{"role": "user", "content": _prompt(candidates, mine, theirs, others, picks_left)}])
        text = "".join(getattr(b, "text", "") for b in msg.content)
    except Exception:
        STATS["failed"] += 1
        return None
    for found in re.findall(r"\[([^\]]+)\]", text) + [text.strip()]:
        if found.strip() in ids:
            STATS["answered"] += 1
            STATS["changed"] += found.strip() != candidates[0][1]["card_id"]
            return found.strip()
    STATS["no_answer"] += 1
    return None
