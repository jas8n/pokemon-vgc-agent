"""Build the static card catalog (pokeagent/data/cards.json) from cards seen in live drafts.

Every card's full set is shown to both players during the draft; this file just remembers them so
the agent knows the exact set of a card it didn't see this match (the opponent's first pick when we
draft second). It is built offline before the submission deadline and never written during play.

    .venv/bin/python tools/build_card_catalog.py ../pokemon-agent/logs
"""
import collections, glob, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pokeagent.sample_cards import SAMPLE_CARDS

logs = sys.argv[1] if len(sys.argv) > 1 else "logs"
sample = {json.dumps(c, sort_keys=True) for c in SAMPLE_CARDS}
FIELDS = ("card_id", "species", "item", "ability", "nature", "level", "evs", "ivs", "moves")
cards, variants = {}, collections.defaultdict(set)


def add(c):
    if not (isinstance(c, dict) and c.get("card_id") and c.get("moves")) or json.dumps(c, sort_keys=True) in sample:
        return
    c = {k: c.get(k) for k in FIELDS}
    variants[c["card_id"]].add(json.dumps(c, sort_keys=True))
    cards[c["card_id"]] = c


for line in open(os.path.join(logs, "cards_seen.jsonl")):
    add(json.loads(line))
for f in glob.glob(os.path.join(logs, "obs_*_draft_p*")):
    for c in json.load(open(f))["observation"].get("available_cards", []):
        add(c)
for f in glob.glob(os.path.join(logs, "match_*.json")):
    for c in json.load(open(f)).get("cards", {}).values():
        add(c)
clashes = [k for k, v in variants.items() if len(v) > 1]
if clashes:
    raise SystemExit(f"cards seen with different sets, not writing a catalog: {clashes}")
out = Path(__file__).resolve().parents[1] / "pokeagent" / "data" / "cards.json"
out.write_text(json.dumps(sorted(cards.values(), key=lambda c: c["card_id"]), indent=1) + "\n")
print(f"wrote {len(cards)} cards to {out}")
