"""판단층(decide.py) 테스트 — 전부 오프라인. API 키·네트워크 불필요.

LLM 응답 자체는 검증 대상이 아니다(정답 없음). 여기서 지키는 건 그 주변의 결정론:
프롬프트에 뭐가 실리나, 실패했을 때 뭘 돌려주나, 월권 행동이 기각되나.
"""
from src.decide import Decision, _fallback, brief
from src.engine import advance_turn
from src.models import Battle, City, Domestic, Faction, GameState, General


def _mini_state() -> GameState:
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=10000, food=5000, gold=5000,
                          wall=1, generals=["조운"]),
            "업":   City(name="업", owner="위", troops=3000, food=2000, gold=4000,
                          generals=["조조"]),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91),
                  "조조": General(name="조조", command=96, is_ruler=True)},
        distances={"성도": {"업": 1}, "업": {"성도": 1}},
    )


def test_brief_shows_own_adjacency_and_enemy():
    s = _mini_state()
    text = brief(s, "촉")
    assert "우리=촉" in text
    assert "성도(촉)" in text and "업(위)" in text        # 양쪽 다 보인다(난이도 보통=전 정보)
    assert "인접: 업(위 1개월)" in text                    # 출병 가능 목적지가 명시된다
    assert "금5000" in text                                # 우리 도시만 금·식량 노출
    assert "금4000" not in text                            # 적 도시 금은 안 실림(토큰 절약)


def test_fallback_is_harmless_and_valid():
    s = _mini_state()
    d = _fallback(s, "촉")
    assert isinstance(d.actions[0], Domestic) and d.actions[0].gold_spent == 0
    before = (s.cities["성도"].gold, s.cities["성도"].food)
    advance_turn(s, {"촉": d.actions})
    assert (s.cities["성도"].gold, s.cities["성도"].food) == before   # 상태 무변


def test_fallback_none_when_no_cities():
    s = _mini_state()
    s.cities["성도"].owner = "위"
    assert _fallback(s, "촉") is None


def test_actor_guard_rejects_foreign_city_order():
    """촉이 위의 도시 '업'에서 출병시키려 하면 기각 — 남의 병력이 움직이면 안 된다."""
    s = _mini_state()
    advance_turn(s, {"촉": Battle(kind="전투", mode="공성", origin="업", target="성도",
                                   troops=3000, generals=["조조"])})
    assert s.operations == []
    assert s.cities["업"].troops == 3000 and s.cities["업"].generals == ["조조"]
    assert any("[위반]" in h and "남의 도시" in h for h in s.history)


def test_actor_guard_rejects_foreign_city_domestic():
    s = _mini_state()
    advance_turn(s, {"촉": Domestic(kind="내정", city="업", item="모병", gold_spent=1000)})
    assert s.cities["업"].troops == 3000 and s.cities["업"].gold == 4000


def test_list_path_still_unguarded():
    """list로 주는 스크립트/테스트 경로는 종전대로 소유권 검증 없음(하위호환)."""
    s = _mini_state()
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="업", target="성도", troops=3000)])
    assert len(s.operations) == 1 and s.operations[0].faction == "위"


def test_decision_wrapper_accepts_all_action_kinds():
    """Union을 property로 내린 래퍼가 네 변형을 다 판별한다(구조화출력 스키마의 형태)."""
    for payload in (
        {"kind": "전투", "mode": "공성", "origin": "성도", "target": "업", "troops": 100},
        {"kind": "내정", "city": "성도", "item": "모병", "gold_spent": 100},
        {"kind": "계략", "target_faction": "위", "scheme_type": "밀정"},
        {"kind": "호송", "origin": "성도", "target": "한중", "troops": 500},
    ):
        d = Decision.model_validate({"actions": [payload]})
        assert d.actions[0].kind == payload["kind"]


def test_rejects_self_siege_but_allows_friendly_field():
    """자국 도시 공성은 기각. 같은 목적지라도 야전(=구원군 방면 출격)은 정당."""
    s = _mini_state()
    s.cities["낙양"] = City(name="낙양", owner="촉", troops=1000)
    s.distances["성도"]["낙양"] = 1
    s.distances["낙양"] = {"성도": 1}

    advance_turn(s, {"촉": Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=1000)})
    assert s.operations == [] and s.cities["성도"].troops == 10000   # 병력도 안 빠져나감

    advance_turn(s, {"촉": Battle(kind="전투", mode="야전", origin="성도", target="낙양", troops=1000)})
    assert any("진군 개시" in h for h in s.history)          # 출격은 성립(구원군)
    assert s.cities["성도"].troops == 10000                   # 거리1 → 같은 턴 도착·대상없음·복귀
