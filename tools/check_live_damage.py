"""Compare our damage calc with the damage Showdown actually dealt in live matches.

For each turn T with a saved board (logs/obs_<session>_battle_tT_*.json) and the next turn's log,
recompute every hit made during turn T from the start-of-turn board and check whether the real HP
lost falls inside our 16-roll range (crits, multi-hit moves and hits after a mid-turn stat change are
skipped or flagged).

    PYTHONPATH=. .venv/bin/python tools/check_live_damage.py
"""

from __future__ import annotations

import glob
import json
import re
from collections import defaultdict

from pokeagent.calc import damage, is_spread
from pokeagent.dex import to_id
from pokeagent.model import Build
from pokeagent.platform import parse_board
from pokeagent.protocol import apply_log, my_player, read_log


def boards():
    out = defaultdict(dict)
    for f in glob.glob("logs/obs_*_battle_t*.json"):
        m = re.match(r"logs/obs_(.+?)_battle_t(\d+)_", f)
        if m and not m.group(1).startswith("fake"):
            out[m.group(1)][int(m.group(2))] = f
    return out


def hp_of(token: str) -> tuple[int, int] | None:
    m = re.match(r"(\d+)/(\d+)", token.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    if token.strip().startswith("0"):
        return 0, 0
    return None


def turn_events(log: list[str], turn: int) -> list[list[str]]:
    out, cur = [], None
    for line in log:
        p = line.split("|")[1:]
        if not p:
            continue
        if p[0] == "turn":
            cur = int(p[1])
            continue
        if cur == turn:
            out.append(p)
    return out


def main() -> None:
    results = []
    for sess, turns in boards().items():
        try:
            previews = sum(1 for line in open(f"logs/decisions_{sess}.jsonl") if '"team_preview"' in line)
        except FileNotFoundError:
            previews = 0
        if previews != 1:
            continue  # self-play: both seats wrote the same files
        try:
            match = json.load(open(f"logs/match_{sess}.json"))
        except FileNotFoundError:
            continue
        cards = match.get("cards", {})
        seen = {to_id(c["species"]): Build.from_card(c) for c in cards.values()}
        mine_sp = {to_id(cards[c]["species"]) for c in match.get("my_ids", []) if c in cards}
        for t, f in sorted(turns.items()):
            nxt = turns.get(t + 1)
            if not nxt:
                continue
            d0 = json.load(open(f))
            obs0, tmpl = d0["observation"], d0["legal_actions"][0]["input"]["action"]
            log1 = json.load(open(nxt))["observation"].get("protocol_log") or []
            me = my_player(obs0)
            view = parse_board(obs0, {k: b for k, b in seen.items() if k in mine_sp},
                               {k: b for k, b in seen.items() if k not in mine_sp}, template=tmpl)
            st = view.state
            apply_log(st, read_log(obs0.get("protocol_log") or []), me)
            hp = {}
            for side in (0, 1):
                for m in st.sides[side].mons:
                    hp[(side, m.sid)] = m
            events = turn_events(log1, t)
            boosted = set()
            for i, p in enumerate(events):
                if p[0] in ("-boost", "-unboost"):
                    boosted.add(p[1][:3])
                if p[0] in ("-heal", "switch", "drag") or (p[0] == "-damage" and any("[from]" in x for x in p[3:])):
                    # keep HP current for indirect damage, healing and mid-turn switch-ins
                    tok = p[1]
                    side = 0 if tok[:2] == me else 1
                    m = hp.get((side, to_id((p[2] if p[0] in ("switch", "drag") else tok).split(",")[0].split(": ")[-1])))
                    h = hp_of(p[3] if p[0] in ("switch", "drag") else p[2])
                    if m is not None and h is not None and h[1]:
                        m.hp = round(h[0] / h[1] * m.maxhp)
                if p[0] != "move" or len(p) < 4 or not p[3].strip():
                    continue
                attacker_tok, move_name, target_tok = p[1], p[2], p[3]
                a_side = 0 if attacker_tok[:2] == me else 1
                a = hp.get((a_side, to_id(attacker_tok.split(": ", 1)[1])))
                spread = any("[spread]" in x for x in p[4:])
                j = i + 1
                hits = []
                while j < len(events) and events[j][0] not in ("move", "switch", "drag", "turn", "upkeep"):
                    e = events[j]
                    if e[0] == "-damage" and len(e) > 2 and not any("[from]" in x for x in e[3:]):
                        hits.append(e)
                    if e[0] in ("-crit", "-hitcount"):
                        hits = None
                        break
                    j += 1
                if not hits or a is None:
                    continue
                for e in hits:
                    d_side = 0 if e[1][:2] == me else 1
                    dmon = hp.get((d_side, to_id(e[1].split(": ", 1)[1])))
                    after = hp_of(e[2])
                    if dmon is None or after is None:
                        continue
                    before_frac = dmon.hp / dmon.maxhp
                    after_frac = after[0] / after[1] if after[1] else 0.0
                    real = before_frac - after_frac
                    mid = to_id(move_name)
                    rolls = damage(st, a_side, a, d_side, dmon, mid, spread=spread or is_spread(mid) and len(
                        [x for x in st.actives() if x[0] == d_side]) > 1)
                    lo, hi = rolls[0] / dmon.maxhp, rolls[-1] / dmon.maxhp
                    tol = 0.011 if d_side == 1 else 1.0 / dmon.maxhp  # opponent HP is shown in whole percent
                    if real < 0:
                        continue  # healed during the hit (drain, berry): not a damage sample
                    ko = after[0] == 0
                    ok = (lo - tol <= real <= hi + tol) or (ko and hi + tol >= real)
                    stale = attacker_tok[:3] in boosted or e[1][:3] in boosted
                    results.append((ok, stale, sess[:8], t, a.name, move_name, dmon.name, real, lo, hi))
                    dmon.hp = max(0, round(after_frac * dmon.maxhp))
    good = sum(1 for r in results if r[0])
    print(f"{good}/{len(results)} hits inside the predicted range")
    for ok, stale, s, t, an, mv, dn, real, lo, hi in results:
        if not ok:
            note = " (stat change earlier this turn)" if stale else ""
            print(f"  {s} T{t}: {an} {mv} -> {dn}: real {real:.1%}, predicted {lo:.1%}-{hi:.1%}{note}")


if __name__ == "__main__":
    main()
