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


def test_battle_log_facts():
    from pokeagent.protocol import apply_log, read_log
    log = ["|start", "|switch|p1a: Sneasler|Sneasler, L50, M|100/100", "|switch|p2a: Dragonite|Dragonite, L50, M|198/198",
           "|switch|p2b: Gholdengo|Gholdengo, L50|162/162", "|-item|p2b: Gholdengo|Air Balloon", "|turn|1",
           "|move|p2a: Dragonite|Tailwind|p2a: Dragonite", "|-sidestart|p2: x|move: Tailwind",
           "|move|p1a: Sneasler|Protect|p1a: Sneasler", "|-singleturn|p1a: Sneasler|Protect",
           "|-fieldstart|move: Trick Room|[of] p1b: Cresselia", "|turn|2",
           "|switch|p2b: Iron Hands|Iron Hands, L50|200/200", "|turn|3"]
    f = read_log(log)
    st = State([Side([Mon.fresh(build("Dragonite")), Mon.fresh(build("Iron Hands"))], [0, 1]),
                Side([Mon.fresh(build("Sneasler"))], [0, None])], Field(), turn=3)
    apply_log(st, f, "p2")
    assert st.sides[0].tailwind == 2          # set on turn 1: active turns 1-4, two left at turn 3
    assert st.field.trickroom == 3
    assert st.sides[0].mons[1].turns_out == 0  # Iron Hands came in during turn 2: Fake Out works on turn 3
    assert st.sides[0].mons[0].turns_out == 2
    assert st.sides[1].mons[0].protect_streak == 0  # protected on turn 1, not turn 2


def test_unseen_opponent_card_gets_a_real_moveset():
    from pokeagent.platform import summary_build
    b = summary_build({"species": "incineroar", "name": "Incineroar", "ability": "intimidate", "moves": []})
    assert "fakeout" in b.moves and len(b.moves) == 4 and b.ability == "intimidate"
    from pokeagent.guess import guess_card, with_revealed
    card = with_revealed(guess_card("incineroar"), ["Knock Off"])
    assert card["moves"][0] == "knockoff" and len(card["moves"]) == 4


def test_make_legal_fixes_illegal_slots():
    from pokeagent.platform import make_legal
    tmpl = {"slots": [
        {"slot": 0, "options": [{"type": "pass"}]},
        {"slot": 1, "force_switch": True, "options": [{"type": "switch", "species": "gyarados"}]}]}
    # the live failure: pass sent for a slot that must switch
    out = make_legal({"type": "doubles_turn", "slot_0": {"type": "pass"}, "slot_1": {"type": "pass"}}, tmpl)
    assert out["slot_1"] == {"type": "switch", "species": "gyarados"} and out["slot_0"] == {"type": "pass"}
    tmpl2 = {"slots": [
        {"slot": 0, "options": [{"type": "move", "move_id": "protect", "targets": []},
                                {"type": "switch", "species": "amoonguss"}]},
        {"slot": 1, "options": [{"type": "move", "move_id": "flareblitz", "targets": [-1, 1, 2]},
                                {"type": "switch", "species": "amoonguss"}]}]}
    # a target not on offer, and both slots switching to the same Pokémon
    out = make_legal({"type": "doubles_turn", "slot_0": {"type": "switch", "species": "amoonguss"},
                      "slot_1": {"type": "switch", "species": "amoonguss"}}, tmpl2)
    assert out["slot_0"]["type"] == "switch" and out["slot_1"] == {"type": "move", "move_id": "flareblitz", "target": 1}
    out = make_legal({"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "protect"},
                      "slot_1": {"type": "move", "move_id": "flareblitz", "target": 0}}, tmpl2)
    assert out["slot_0"] == {"type": "move", "move_id": "protect"} and out["slot_1"]["target"] == 1


def test_no_taunt_on_our_own_partner():
    from pokeagent.platform import prune_ally_hits
    slot0 = [("move", "taunt", -2), ("move", "taunt", 1), ("move", "helpinghand", -2), ("move", "flareblitz", -2),
             ("move", "flareblitz", 2)]
    kept = prune_ally_hits([slot0, [("pass",)]])[0]
    assert ("move", "taunt", -2) not in kept and ("move", "flareblitz", -2) not in kept
    assert ("move", "helpinghand", -2) in kept and ("move", "taunt", 1) in kept


def test_draft_only_picks_from_the_legal_list():
    import agent.agent as am
    from altruagent import DecisionContext, GameState, LegalAction, WithReasoning
    am.LOG_DIR = am.LOG_DIR.parent / "logs_test"
    cards = [CARDS["Incineroar"], CARDS["Flutter Mane"], CARDS["Amoonguss"]]
    legal = [c for c in cards if c["species"] != "Incineroar"]  # Incineroar blocked (e.g. Item Clause)
    obs = {"phase": "draft", "pick_number": 3, "available_cards": cards, "rosters": {}}
    st = GameState.from_mcp_state({"session_id": "s", "game_type": "pokemon_vgc_doubles_draft", "phase": "draft",
                                   "observation": obs, "is_current_actor": True, "legal_actions": {"actions": [
                                       {"action_id": f"draft_pick:{c['card_id']}", "label": c["species"],
                                        "input": {"action": {"type": "draft_pick", "card_id": c["card_id"]}}}
                                       for c in legal], "state_version": 2}})
    out = am.PokemonAgent().choose_action(st, DecisionContext(session_id="s", tournament_id=None,
                                                              game_type="pokemon_vgc_doubles_draft", agent_id="me"))
    action = out.action if isinstance(out, WithReasoning) else out
    assert isinstance(action, LegalAction) and action.action_id in {f"draft_pick:{c['card_id']}" for c in legal}
    import shutil
    shutil.rmtree(am.LOG_DIR, ignore_errors=True)


def test_left_behind_pokemon_are_not_in_the_battle():
    from agent.agent import PokemonAgent
    a = PokemonAgent()
    a.brought = ["incineroar", "rillaboom"]
    st = State([Side([Mon.fresh(build("Incineroar")), Mon.fresh(build("Rillaboom")), Mon.fresh(build("Amoonguss"))],
                     [0, 1]), Side([Mon.fresh(build("Urshifu-Rapid-Strike"))], [0, None])], Field())
    a._mark_left_behind(st, {"team": {}}, {"slots": []}, None)
    assert st.sides[0].mons[2].vol.get("not_brought") and st.sides[0].bench() == [] and st.sides[0].alive_count() == 2
    # without preview memory, the battle itself shows what was brought
    b = PokemonAgent()
    st2 = State([Side([Mon.fresh(build("Incineroar")), Mon.fresh(build("Rillaboom")), Mon.fresh(build("Amoonguss")),
                       Mon.fresh(build("Gyarados"))], [0, 1]), Side([Mon.fresh(build("Urshifu-Rapid-Strike"))], [0, None])], Field())
    tmpl = {"slots": [{"slot": 0, "active": {"species": "incineroar"}, "options": [{"type": "switch", "species": "gyarados"}]},
                      {"slot": 1, "active": {"species": "rillaboom"}, "options": []}]}
    b._mark_left_behind(st2, {"team": {}}, tmpl, None)
    assert st2.sides[0].mons[2].vol.get("not_brought") and not st2.sides[0].mons[3].vol.get("not_brought")
