"""Optional LLM Team Preview advisor: Claude chooses which 4 to bring and which 2 to lead.

Claude sees both full teams (exact sets and level-50 stats) and our simulator's choice, and may pick
any legal lineup. It runs in a background thread while our own simulation runs, so it adds little
time. Any failure (no key, timeout, an answer that is not a legal lineup) keeps our choice.
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter

from . import llm_draft
from .model import Build

MODEL = "claude-opus-5-5"
EFFORT = "medium"
TIMEOUT_S = 30.0  # the platform allows 300 s per decision; Showdown 420 s per battle
STATS: Counter = Counter()  # answered / changed / failed / no_answer, for offline reports


def _describe(b: Build) -> str:
    s = b.stats
    return (f"{b.name} @ {b.item or 'no item'} | {b.ability} | {', '.join(b.moves)} | "
            f"HP {s['hp']} Atk {s['atk']} Def {s['def']} SpA {s['spa']} SpD {s['spd']} Spe {s['spe']}")


def _prompt(mine: list[Build], theirs: list[Build], our_bring: list[int], our_leads: list[int]) -> str:
    ours = "\n".join(f"- {_describe(b)}" for b in mine)
    opp = "\n".join(f"- {_describe(b)}" for b in theirs)
    return f"""Pokémon VGC doubles Team Preview: Gen 9, Regulation I, level 50, no Terastallization. Each \
player brings 4 of their 6 and leads with 2 of those 4. Stats shown are actual level-50 stats.

Our team:
{ours}

Opponent's team (they will also bring 4 of these 6):
{opp}

Our simulator suggests bringing {', '.join(mine[i].name for i in our_bring)} and leading \
{mine[our_leads[0]].name} + {mine[our_leads[1]].name}.

Choose the 4 to bring and the 2 leads that give us the best chance to win. Think about what the \
opponent will likely bring and lead, speed control (Tailwind, Trick Room, priority), Fake Out and \
redirection, Intimidate, spread moves, type matchups, and which of our Pokémon are win conditions. \
The suggestion is a reasonable default; change it only if you see a clearly better plan.

End your answer with one line of JSON using our Pokémon's names exactly as written above:
{{"bring": ["A", "B", "C", "D"], "lead": ["A", "B"]}}"""


def _parse(text: str, mine: list[Build]) -> tuple[list[int], list[int]] | None:
    for raw in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        names = {b.name.lower(): i for i, b in enumerate(mine)}
        try:
            bring = [names[str(n).lower()] for n in d["bring"]]
            lead = [names[str(n).lower()] for n in d["lead"]]
        except (KeyError, TypeError):
            continue
        if len(set(bring)) == 4 and len(set(lead)) == 2 and set(lead) <= set(bring):
            return bring, lead
    return None


class PreviewAdvice:
    """Starts the Claude call at once; result() waits for it until the deadline."""

    def __init__(self, mine: list[Build], theirs: list[Build]):
        self.mine, self.theirs = mine, theirs
        self.text: str | None = None
        self.failed = False
        self.client = llm_draft._get_client()
        self.thread = None

    def start(self, our_bring: list[int], our_leads: list[int]):
        if self.client is None or len(self.mine) < 4:
            return self

        def run():
            try:
                msg = self.client.messages.create(
                    model=MODEL, max_tokens=6000, timeout=TIMEOUT_S,
                    extra_body={"output_config": {"effort": EFFORT}},
                    messages=[{"role": "user", "content": _prompt(self.mine, self.theirs, our_bring, our_leads)}])
                self.text = "".join(getattr(b, "text", "") for b in msg.content)
            except Exception:
                self.failed = True
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def result(self, wait_s: float, our_bring: list[int], our_leads: list[int]):
        """(bring, leads) from Claude, or None to keep ours."""
        if self.thread is None:
            return None
        self.thread.join(max(0.0, wait_s))
        if self.thread.is_alive() or self.failed or self.text is None:
            STATS["failed"] += 1
            return None
        got = _parse(self.text, self.mine)
        if got is None:
            STATS["no_answer"] += 1
            return None
        STATS["answered"] += 1
        STATS["changed"] += (set(got[0]) != set(our_bring)) or (set(got[1]) != set(our_leads))
        return got
