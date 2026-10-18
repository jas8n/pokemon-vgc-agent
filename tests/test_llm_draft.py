from types import SimpleNamespace

from pokeagent import llm_draft


def _card(cid, species):
    return {"card_id": cid, "species": species, "item": "Leftovers", "ability": "?", "moves": ["Protect"]}


RANKED = [(10.0, _card("a", "Archaludon")), (9.0, _card("b", "Amoonguss")),
          (8.0, _card("c", "Incineroar")), (7.0, _card("d", "Rillaboom")), (6.0, _card("e", "Kingambit"))]


class FakeClient:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.prompts = text, error, []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)])


def _with(monkeypatch, client):
    monkeypatch.setattr(llm_draft, "_client", client)
    monkeypatch.setattr(llm_draft, "_disabled", client is None)


def test_picks_a_candidate(monkeypatch):
    _with(monkeypatch, FakeClient("[c]"))
    assert llm_draft.advise(RANKED, [], [], [c for _, c in RANKED], 6) == "c"


def test_only_top_candidates_are_allowed(monkeypatch):
    _with(monkeypatch, FakeClient("[e]"))  # fifth-ranked: outside the shortlist
    assert llm_draft.advise(RANKED, [], [], [c for _, c in RANKED], 6) is None


def test_bare_id_answer(monkeypatch):
    _with(monkeypatch, FakeClient(" b \n"))
    assert llm_draft.advise(RANKED, [], [], [c for _, c in RANKED], 6) == "b"


def test_errors_and_missing_key_fall_back(monkeypatch):
    _with(monkeypatch, FakeClient(error=TimeoutError()))
    assert llm_draft.advise(RANKED, [], [], [], 6) is None
    _with(monkeypatch, None)
    assert llm_draft.advise(RANKED, [], [], [], 6) is None


def test_prompt_lists_candidates_and_teams(monkeypatch):
    fake = FakeClient("[a]")
    _with(monkeypatch, fake)
    llm_draft.advise(RANKED, [_card("x", "Tornadus")], [_card("y", "Urshifu")], [c for _, c in RANKED], 5)
    p = fake.prompts[0]
    assert "[a]" in p and "[d]" in p and "[e]" not in p
    assert "Tornadus" in p and "Urshifu" in p and "5 picks left" in p


def test_preview_answer_must_be_a_legal_lineup():
    import json
    from pathlib import Path
    from pokeagent import llm_preview
    from pokeagent.model import Build
    cards = json.loads((Path(llm_preview.__file__).parent / "data" / "cards.json").read_text())
    mine = [Build.from_card(c) for c in cards[:6]]
    names = [b.name for b in mine]
    ok = f'{{"bring": ["{names[0]}", "{names[1]}", "{names[2]}", "{names[3]}"], "lead": ["{names[0]}", "{names[2]}"]}}'
    assert llm_preview._parse("thinking...\n" + ok, mine) == ([0, 1, 2, 3], [0, 2])
    lead_not_brought = ok.replace(f'"lead": ["{names[0]}"', f'"lead": ["{names[5]}"')
    assert llm_preview._parse(lead_not_brought, mine) is None
    assert llm_preview._parse('{"bring": ["Mew"], "lead": []}', mine) is None


def test_battle_advisor_needs_close_plans_and_a_client(monkeypatch):
    from pokeagent import llm_battle, llm_draft
    monkeypatch.setattr(llm_draft, "_client", None)
    monkeypatch.setattr(llm_draft, "_disabled", True)
    adv = llm_battle.BattleAdvisor()
    plans = [((("move", "protect", 0), ("pass",)), 10.0), ((("move", "tackle", 1), ("pass",)), 5.0)]
    assert adv.choose(None, plans[:1], []) is None      # nothing to choose between
    assert adv.choose(None, plans, []) is None          # no API client: keep the search's pick


def test_llm_modes_default_off(monkeypatch):
    import agent.agent as am
    monkeypatch.setenv("POKEAGENT_LLM", "")
    assert am._llm_modes() == set()
    monkeypatch.setenv("POKEAGENT_LLM", "battle, draft")
    assert am._llm_modes() == {"battle", "draft"}
    monkeypatch.setenv("POKEAGENT_LLM", "all")
    assert am._llm_modes() == {"draft", "preview", "battle"}
