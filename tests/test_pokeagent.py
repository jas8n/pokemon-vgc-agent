"""Tests for the pure-code Pokémon agent (pokeagent/ and agent/agent.py)."""

from pokeagent.calc import damage
from pokeagent.dex import to_id
from pokeagent.model import Build, Field, Mon, Side, State
from pokeagent.platform import parse_board, parse_field, template_options, to_platform
from pokeagent.sample_cards import SAMPLE_CARDS
from pokeagent.search import decide

CARDS = {c["species"]: c for c in SAMPLE_CARDS}


def build(name):
    return Build.from_card(CARDS[name])


def test_stats_match_known_values():
    # 252 Atk Adamant Urshifu-RS at level 50: 200 Atk; 4 HP -> 176 HP
    b = build("Urshifu-Rapid-Strike")
    assert b.stats["atk"] == 200 and b.stats["hp"] == 176


def test_spread_and_immunity():
    chi, garchomp, flutter = Mon.fresh(build("Chi-Yu")), Mon.fresh(build("Garchomp")), Mon.fresh(build("Flutter Mane"))
    st = State([Side([chi], [0, None]), Side([garchomp, flutter], [0, 1])], Field())
    single = damage(st, 0, chi, 1, flutter, "heatwave")
    spread = damage(st, 0, chi, 1, flutter, "heatwave", spread=True)
    assert spread[-1] < single[-1]
    # Earthquake does nothing to Flutter Mane (Ground vs Levitate-less Ghost/Fairy is fine, Flying isn't);
    st2 = State([Side([garchomp], [0, None]), Side([Mon.fresh(build("Tornadus"))], [0, None])], Field())
    assert max(damage(st2, 0, garchomp, 1, st2.sides[1].mons[0], "earthquake")) == 0


def _obs_variant(status, weather_key, hp_style):
    def summ(name, hp, mx, opp):
        d = {"species": to_id(name), "name": name, "status": status if name == "Incineroar" else None,
             "boosts": {"atk": -1} if name == "Urshifu-Rapid-Strike" else {}, "fainted": False, "revealed": True}
        if hp_style == "fraction":
            d["current_hp_fraction"] = hp / mx
        else:
            d["current_hp"], d["max_hp"] = (round(100 * hp / mx), 100) if opp else (hp, mx)
        return d
    return {
        "turn": 3, "is_doubles": True, "weather": {weather_key: 2}, "fields": ["Field.TRICK_ROOM"],
        "active_pokemon": [summ("Incineroar", 150, 202, False), summ("Rillaboom", 100, 207, False)],
        "opponent_active_pokemon": [summ("Urshifu-Rapid-Strike", 88, 176, True), None],
        "team": {"p1: Incineroar": summ("Incineroar", 150, 202, False), "p1: Rillaboom": summ("Rillaboom", 100, 207, False),
                 "p1: Amoonguss": summ("Amoonguss", 221, 221, False)},
        "opponent_team": {"p2: Urshifu-Rapid-Strike": summ("Urshifu-Rapid-Strike", 88, 176, True)},
        "side_conditions": {"SideCondition.TAILWIND": 2}, "opponent_side_conditions": {},
    }


def test_parse_board_tolerates_spellings():
    mine = {b.sid: b for b in map(build, ("Incineroar", "Rillaboom", "Amoonguss"))}
    theirs = {b.sid: b for b in map(build, ("Urshifu-Rapid-Strike", "Flutter Mane"))}
    for status, wkey, hp in (("BRN", "Weather.SUNNYDAY", "fraction"), ("brn", "SUNNYDAY", "absolute"),
                             ("Status.BRN", "sunnyday", "absolute")):
        view = parse_board(_obs_variant(status, wkey, hp), mine, theirs)
        st = view.state
        assert st.field.weather == "sun" and st.field.trickroom > 0
        assert st.sides[0].tailwind > 0
        inc = st.sides[0].active_mon(0)
        assert inc.name == "Incineroar" and inc.status == "brn"
        urs = st.sides[1].active_mon(0)
        assert urs.boosts["atk"] == -1 and abs(urs.hp_frac - 0.5) < 0.02
        assert st.sides[1].active_mon(1) is None
        assert st.sides[1].unseen == 3


def test_template_round_trip_and_decide():
    mine = {b.sid: b for b in map(build, ("Incineroar", "Rillaboom", "Amoonguss"))}
    theirs = {b.sid: b for b in map(build, ("Urshifu-Rapid-Strike", "Flutter Mane"))}
    obs = _obs_variant(None, "RAINDANCE", "absolute")
    obs["opponent_active_pokemon"][1] = {"species": "fluttermane", "name": "Flutter Mane", "current_hp_fraction": 1.0}
    tmpl = {"type": "doubles_turn", "slots": [
        {"slot": 0, "board_position": -1, "force_switch": False, "options": [
            {"type": "move", "move_id": "flareblitz", "targets": [-2, 1, 2], "target_options": [
                {"target": -2, "side": "ally", "species": "rillaboom"},
                {"target": 1, "side": "opponent", "species": "fluttermane"},
                {"target": 2, "side": "opponent", "species": "urshifurapidstrike"}]},
            {"type": "move", "move_id": "partingshot", "targets": [1, 2]},
            {"type": "switch", "species": "amoonguss"}]},
        {"slot": 1, "board_position": -2, "force_switch": False, "options": [
            {"type": "move", "move_id": "woodhammer", "targets": [-1, 1, 2]},
            {"type": "move", "move_id": "grassyglide", "targets": [-1, 1, 2]},
            {"type": "switch", "species": "amoonguss"}]}]}
    view = parse_board(obs, mine, theirs, template=tmpl)
    # target_options say Flutter Mane is in position A (1) even though the list order said otherwise
    assert view.state.sides[1].active_mon(0).name == "Flutter Mane"
    per_slot, back = template_options(view, tmpl)
    choice = decide(view.state, per_slot, side=0, budget_s=5)
    out = to_platform(choice, back)
    assert out["type"] == "doubles_turn"
    for n in (0, 1):
        s = out[f"slot_{n}"]
        assert s["type"] in ("move", "switch")
        assert not (out["slot_0"]["type"] == out["slot_1"]["type"] == "switch"
                    and out["slot_0"]["species"] == out["slot_1"]["species"])


def test_parse_field_variants():
    assert parse_field({"weather": "Weather.RAINDANCE"}, 1).weather == "rain"
    assert parse_field({"fields": {"ELECTRIC_TERRAIN": 1}}, 1).terrain == "electric"
    assert parse_field({}, 1).weather == ""
