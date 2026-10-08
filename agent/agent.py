"""Our tournament agent for pokemon_vgc_doubles_draft (pure code: damage calc + search).

    python -m agent --match         # test matches
    python -m agent --tournament    # tournament games

Each phase hands off to pokeagent/:
  draft         pokeagent.draft.choose_pick      (15 s clock per pick)
  team_preview  pokeagent.preview.choose_lineup  (simulates candidate fours against theirs)
  moving        pokeagent.search.decide           (every pairing of our options vs theirs)

Every match starts with empty memory (Official Rules section 5). Within a match, each card we see
is kept in logs/match_<id>_<seat>.json, because roster entries only carry card ids and the full sets
appear only while they're on offer; that file is only for resuming the same match after a crash.
logs/cards_seen.jsonl is written for offline analysis and is never read by the agent.
logs/decisions_<id>_<seat>.jsonl records every action sent, with timestamps (section 9 records).
Any exception falls back to a legal default so a bug never costs a turn. Other games (Werewolf)
get the first legal action, like the starter placeholder.
"""

from __future__ import annotations

import json
import random
import time
import traceback
from itertools import product
from pathlib import Path

from altruagent import DecisionContext, GameState, WithReasoning
from examples import smoke_agent
from pokeagent import draft as drafting
from pokeagent import preview as previewing
from pokeagent.dex import to_id
from pokeagent.engine import PROTECT_MOVES, Resolver, move_data, slot_options
from pokeagent.model import Build
from pokeagent.platform import parse_board, prune_ally_hits, template_options, to_platform
from pokeagent.protocol import apply_log, my_player, read_log
from pokeagent.search import decide

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
CATALOG = LOG_DIR / "cards_seen.jsonl"
# Showdown's own timer: 90 s per turn and 420 s for the whole battle, so stay well inside it
PREVIEW_BUDGET_S = 15.0
TURN_BUDGET_S = 8.0
DEFAULT_EVS = {s: 84 for s in ("hp", "atk", "def", "spa", "spd", "spe")}


def _log(msg: str) -> None:
    print(f"[pokeagent] {msg}", flush=True)


class PokemonAgent:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}   # card_id -> full card, this match only
        self.catalog_ids: set[str] = set()
        self.session = ""
        self.me = ""
        self.my_ids: list[str] = []
        self.opp_ids: list[str] = []
        self.opp_preview: list[dict] = []
        self.came_in: dict[str, int] = {}  # "side:species" -> turn it was first seen active this stint
        self.protects: dict[str, tuple[int, int]] = {}  # our species -> (turn of last Protect, streak)
        self.rng = random.Random()

    # ---------------- persistence ----------------
    def _match_file(self) -> Path:
        return LOG_DIR / f"match_{self.session}_{self.me[:8]}.json"  # one file per seat

    def _save(self) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            keep = set(self.my_ids + self.opp_ids)
            self._match_file().write_text(json.dumps({
                "me": self.me, "my_ids": self.my_ids, "opp_ids": self.opp_ids,
                "cards": {k: v for k, v in self.cards.items() if k in keep},
                "opp_preview": self.opp_preview,
            }))
        except Exception:
            pass

    def _restore(self, session: str) -> None:
        if session == self.session:
            return
        self.session = session
        try:
            data = json.loads(self._match_file().read_text())
            self.me = data.get("me", self.me)
            self.my_ids = data.get("my_ids", [])
            self.opp_ids = data.get("opp_ids", [])
            self.cards.update(data.get("cards", {}))
            self.opp_preview = data.get("opp_preview", [])
        except Exception:
            pass

    def _remember_cards(self, cards: list[dict]) -> None:
        new = []
        for c in cards:
            if isinstance(c, dict) and c.get("card_id") and c.get("moves"):
                self.cards[c["card_id"]] = c
                if c["card_id"] not in self.catalog_ids:
                    self.catalog_ids.add(c["card_id"])
                    new.append(c)
        if new:
            try:
                LOG_DIR.mkdir(exist_ok=True)
                with CATALOG.open("a") as f:
                    for c in new:
                        f.write(json.dumps(c) + "\n")
            except Exception:
                pass

    # ---------------- entry point ----------------
    def choose_action(self, state: GameState, context: DecisionContext):
        if not (context.game_type or state.game_name or "").startswith("pokemon"):
            return state.legal_actions[0]
        self.me = context.agent_id or self.me
        self._restore(state.session_id or context.session_id)
        obs = state.raw.get("observation") if isinstance(state.raw, dict) else None
        if not isinstance(obs, dict):
            try:
                obs = json.loads(state.observation)
            except Exception:
                obs = {}
        phase = state.phase or obs.get("phase")
        t0 = time.time()
        try:
            if phase == "draft":
                result = self._draft(state, obs)
            elif phase == "team_preview":
                result = self._preview(state, obs)
            elif phase == "moving":
                result = self._battle(state, obs)
            else:
                result = smoke_agent.choose_action(state, None)
        except Exception:
            _log(f"{phase}: error, playing a safe default\n{traceback.format_exc()}")
            self._dump(obs, state, "error")
            result = WithReasoning(smoke_agent.choose_action(state, None), "Playing a safe default move.")
        _log(f"{phase} decided in {time.time() - t0:.1f}s")
        self._record(state, phase, result, time.time() - t0)
        return result

    def _record(self, state: GameState, phase: str, result, seconds: float) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            action = result.action if isinstance(result, WithReasoning) else result
            reason = result.reasoning_summary if isinstance(result, WithReasoning) else None
            if hasattr(action, "action_id"):
                action = {"action_id": action.action_id}
            with (LOG_DIR / f"decisions_{self.session}_{self.me[:8]}.jsonl").open("a") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phase": phase,
                                    "state_version": state.state_version, "seconds": round(seconds, 2),
                                    "action": action, "reason": reason}, default=str) + "\n")
        except Exception:
            pass

    def _dump(self, obs: dict, state: GameState, tag: str) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / f"obs_{self.session}_{self.me[:8]}_{tag}_{int(time.time() * 1000)}.json"
            path.write_text(json.dumps({"observation": obs, "legal_actions": [a.raw for a in state.legal_actions]},
                                       default=str, indent=1))
        except Exception:
            pass

    # ---------------- draft ----------------
    def _draft(self, state: GameState, obs: dict):
        available = [c for c in obs.get("available_cards") or [] if isinstance(c, dict)]
        self._remember_cards(available)
        rosters = obs.get("rosters") or {}
        my_entries = rosters.get(self.me, [])
        their_entries = [r for k, rs in rosters.items() if k != self.me for r in rs]
        self.my_ids = [r.get("card_id") for r in my_entries]
        self.opp_ids = [r.get("card_id") for r in their_entries]
        self._save()
        legal_ids = {a.action_id.split(":", 1)[1] for a in state.legal_actions if a.action_id.startswith("draft_pick:")}
        offer = [c for c in available if c["card_id"] in legal_ids] or available
        mine = [c for c in (self._card(r.get("card_id"), r) for r in my_entries) if c]
        theirs = [c for c in (self._card(r.get("card_id"), r) for r in their_entries) if c]
        card_id, reason = drafting.choose_pick(offer, mine, theirs, available + mine + theirs,
                                               picks_left_mine=6 - len(mine))
        if card_id not in self.my_ids:
            self.my_ids.append(card_id)
            self._save()
        _log(f"draft pick {obs.get('pick_number')}: {card_id} — {reason}")
        return WithReasoning({"type": "draft_pick", "card_id": card_id}, reason)

    def _card(self, card_id: str | None, roster_entry: dict | None = None) -> dict | None:
        if card_id and card_id in self.cards:
            return self.cards[card_id]
        species = (roster_entry or {}).get("species")
        if not species:
            return None
        # A set we never saw (picked before our first look): reuse a known card of that species
        for c in self.cards.values():
            if to_id(c.get("species")) == to_id(species):
                return c
        return {"card_id": card_id or to_id(species), "species": species, "item": "", "ability": "",
                "nature": "Serious", "level": 50, "evs": DEFAULT_EVS, "moves": []}

    # ---------------- team preview ----------------
    def _builds_from_preview(self, roster: list[dict], ids: list[str]) -> list[Build]:
        """Builds for a preview roster, preferring the drafted cards, then what the preview shows."""
        by_species = {to_id(c["species"]): c for c in self.cards.values()}  # every card seen this match
        for cid in ids:  # cards known to be on this roster win any species clash
            c = self.cards.get(cid)
            if c:
                by_species[to_id(c["species"])] = c
        out = []
        for entry in roster:
            key = to_id(entry.get("species") or entry.get("name"))
            card = by_species.get(key) or next(
                (c for k, c in by_species.items() if k.startswith(key) or key.startswith(k)), None)
            if card is None:
                moves = entry.get("moves") or []
                card = {"card_id": key, "species": entry.get("species") or entry.get("name"),
                        "item": entry.get("item") or "", "ability": entry.get("ability") or "",
                        "nature": entry.get("nature") or "Serious", "level": entry.get("level") or 50,
                        "evs": entry.get("evs") or DEFAULT_EVS,
                        "moves": list(moves) if isinstance(moves, dict) else moves}
            out.append(Build.from_card(card))
        return out

    def _preview(self, state: GameState, obs: dict):
        self._dump(obs, state, "preview")
        template = state.legal_actions[0].input.get("action") or {}
        roster_ids = list(dict.fromkeys(template.get("roster") or []))
        your = obs.get("your_roster") or [{"species": s} for s in roster_ids]
        opp = obs.get("opponent_roster") or []
        self.opp_preview = opp
        self._save()
        mine = self._builds_from_preview(your, self.my_ids)
        theirs = self._builds_from_preview(opp, self.opp_ids)
        bring, leads = previewing.choose_lineup(mine, theirs, budget_s=PREVIEW_BUDGET_S, rng=self.rng)

        def species_id(i: int) -> str:
            key = to_id(your[i].get("species") or your[i].get("name"))
            return next((r for r in roster_ids if to_id(r) == key), key)

        reason = (f"Bringing {', '.join(mine[i].name for i in bring)}; "
                  f"leading {mine[leads[0]].name} + {mine[leads[1]].name}.")
        _log(f"preview: {reason}")
        return WithReasoning({"type": "select_lineup", "bring": [species_id(i) for i in bring],
                              "leads": [species_id(i) for i in leads]}, reason)

    # ---------------- battle ----------------
    def _battle(self, state: GameState, obs: dict):
        template = state.legal_actions[0].input.get("action") or {}
        if template.get("type") != "doubles_turn":
            return smoke_agent.choose_action(state, None)
        self._dump(obs, state, f"battle_t{obs.get('turn')}")
        seen = {to_id(c["species"]): Build.from_card(c) for c in self.cards.values() if c.get("species")}
        mine_sp = {to_id(self.cards[c]["species"]) for c in self.my_ids if c in self.cards}
        my_builds = {k: b for k, b in seen.items() if k in mine_sp} or seen
        opp_builds = {k: b for k, b in seen.items() if k not in mine_sp}
        view = parse_board(obs, my_builds, opp_builds, template=template)
        st = view.state
        log = obs.get("protocol_log")
        if log:
            apply_log(st, read_log(log), my_player(obs))
        else:
            self._track_turns(st)

        if not log:
            self._apply_protect_streaks(st)
        per_slot, back = template_options(view, template)
        per_slot = prune_ally_hits(per_slot)
        slots = sorted(template.get("slots") or [], key=lambda s: s.get("slot", 0))
        if any(bool(s.get("force_switch")) for s in slots):
            choice = self._forced_switch(st, per_slot)
            reason = "Bringing in " + " and ".join(
                st.sides[0].mons[a[1]].name for a in choice if a[0] == "switch") + "."
        else:
            choice, ranked, likely = decide(st, per_slot, side=0, budget_s=TURN_BUDGET_S, return_scores=True, depth=2)
            reason = self._explain(st, choice)
            self._record_protects(st, choice)
            _log(f"turn {st.turn}: {choice} | expect opp {likely[0][0] if likely else '?'}")
        return WithReasoning(to_platform(choice, back), reason)

    def _track_turns(self, st) -> None:
        """Fake Out only works on a Pokémon's first turn out; remember when each one came in."""
        current = {}
        for side in (0, 1):
            for slot in (0, 1):
                mon = st.sides[side].active_mon(slot)
                if mon is None:
                    continue
                key = f"{side}:{mon.sid}"
                current[key] = self.came_in.get(key, st.turn)
                mon.turns_out = max(mon.turns_out, st.turn - current[key])
        self.came_in = current

    def _apply_protect_streaks(self, st) -> None:
        for slot in (0, 1):
            mon = st.sides[0].active_mon(slot)
            if mon is not None and mon.sid in self.protects:
                turn, streak = self.protects[mon.sid]
                if turn == st.turn - 1:
                    mon.protect_streak = streak

    def _record_protects(self, st, choice) -> None:
        for slot, act in enumerate(choice):
            mon = st.sides[0].active_mon(slot)
            if mon is None:
                continue
            if act[0] == "move" and act[1] in PROTECT_MOVES:
                self.protects[mon.sid] = (st.turn, mon.protect_streak + 1)
            else:
                self.protects.pop(mon.sid, None)

    def _forced_switch(self, st, per_slot):
        """Pick replacements by the value of the position they create for next turn."""
        best, best_v = None, -1e9
        for a, b in product(per_slot[0], per_slot[1]):
            if a[0] == b[0] == "switch" and a[1] == b[1]:
                continue
            if a[0] == b[0] == "pass" and (len(per_slot[0]) > 1 or len(per_slot[1]) > 1):
                continue
            nxt = st.copy()
            r = Resolver(nxt, None)
            for slot, act in ((0, a), (1, b)):
                if act[0] == "switch":
                    nxt.sides[0].active[slot] = act[1]
                    nxt.sides[0].mons[act[1]].turns_out = 0
                    r.on_entry(0, slot)
            ours = [slot_options(nxt, 0, 0), slot_options(nxt, 0, 1)]
            _, ranked, _ = decide(nxt, ours, side=0, budget_s=3, keep=4, return_scores=True)
            v = ranked[0][1] if ranked else -1e9
            if v > best_v:
                best, best_v = (a, b), v
        return best or (per_slot[0][0], per_slot[1][0])

    def _explain(self, st, choice) -> str:
        parts = []
        for slot, act in enumerate(choice):
            mon = st.sides[0].active_mon(slot)
            if mon is None or act[0] == "pass":
                continue
            if act[0] == "switch":
                parts.append(f"{mon.name} switches to {st.sides[0].mons[act[1]].name}")
                continue
            name = move_data(act[1]).get("name", act[1])
            tgt = ""
            if act[2] in (1, 2):
                t = st.sides[1].active_mon(act[2] - 1)
                tgt = f" into {t.name}" if t else ""
            elif act[2] in (-1, -2) and -act[2] - 1 != slot:
                t = st.sides[0].active_mon(-act[2] - 1)
                tgt = f" on {t.name}" if t else ""
            parts.append(f"{mon.name} uses {name}{tgt}")
        return ("; ".join(parts) + ".") if parts else "Best option by damage calc and search."


def create_agent():
    return PokemonAgent()
