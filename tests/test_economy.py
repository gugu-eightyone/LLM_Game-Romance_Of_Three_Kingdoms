"""경제 틱(매턴 수입·병량 소모·탈영) — 전부 오프라인. ⭐사용자 2026-08-30 "금은 언제 올라?"."""
from src.config import (CITY_INCOME_FOOD, CITY_INCOME_GOLD, GARRISON_TROOPS_PER_FOOD,
                        STRANDED_DESERTION, TROOPS_PER_FOOD)
from src.engine import _economy_tick, advance_turn, food_runway, load_scenario
from src.models import ActiveOperation, Battle, City, Faction, GameState


def _state(**city_kw):
    return GameState(
        cities={"성도": City(name="성도", owner="촉", **city_kw)},
        factions={"촉": Faction(name="촉", ruler="유비")},
        distances={"성도": {}},
    )


def test_income_and_upkeep():
    s = _state(size=2, troops=2000, food=1000, gold=0)
    advance_turn(s, [])
    assert s.cities["성도"].gold == 2 * CITY_INCOME_GOLD
    # 식량 = 기존 + 수입 − 주둔 병량(⭐차등: 40명당 1 = 원정의 절반)
    assert s.cities["성도"].food == 1000 + 2 * CITY_INCOME_FOOD - 2000 // GARRISON_TROOPS_PER_FOOD


def test_starvation_desertion():
    s = _state(size=1, troops=40000, food=0)
    advance_turn(s, [])
    c = s.cities["성도"]
    # 수입 500 < 주둔 병량 1000 → 부족 500×40=20000 탈영, 식량 0으로 소진
    assert c.food == 0 and c.troops == 40000 - (
        40000 // GARRISON_TROOPS_PER_FOOD - CITY_INCOME_FOOD) * GARRISON_TROOPS_PER_FOOD
    assert any("[병량]" in h and "탈영" in h for h in s.history)


def test_size_zero_no_economy():
    s = _state(size=0, troops=5000, food=0, gold=7)
    advance_turn(s, [])
    c = s.cities["성도"]
    assert (c.gold, c.food, c.troops) == (7, 0, 5000)   # 규모 미설정=경제 없음(기존 테스트 보호막)


def test_scenario_has_sizes():
    s = load_scenario()
    assert all(c.size >= 1 for c in s.cities.values())   # 실 시나리오는 전 도시 규모 큐레이트


def test_scenario_food_is_12_months():
    s = load_scenario()   # ⭐시작 비축=주둔 월소비×12(사용자 2026-09-02) — 구 "비축≈병력수"는 탈영 무실효
    assert all(c.food == (c.troops // GARRISON_TROOPS_PER_FOOD) * 12 for c in s.cities.values())


def _war_state(**city_kw):
    return GameState(
        cities={"성도": City(name="성도", owner="촉", **city_kw),
                "장안": City(name="장안", owner="위", troops=1)},
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        distances={"성도": {"장안": 2}, "장안": {"성도": 2}},
    )


def test_field_army_upkeep_charged_to_origin():
    # ⭐출전 부대 병량=균일·출발지 청구(사용자 2026-09-02). 이동 중(거리 2)에도 창고를 축낸다.
    s = _war_state(size=2, troops=10000, food=5000, gold=0)
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="장안", troops=4000)])
    c = s.cities["성도"]
    # 식량 = 5000 + 수입 1000 − 주둔 6000/40 − 출전 4000/20 (⭐원정=주둔의 2배 소모율)
    assert c.food == (5000 + 2 * CITY_INCOME_FOOD
                      - 6000 // GARRISON_TROOPS_PER_FOOD - 4000 // TROOPS_PER_FOOD)


def test_field_army_starvation_deserts_op():
    # 창고 고갈 시 탈영은 원정군에서(주둔은 먼저 먹음). 잔여 250 < 원정 500 → 부족 250×20=5000 탈영.
    s = _war_state(size=1, troops=20000, food=0, gold=0)
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="장안", troops=10000)])
    op = s.operations[0]
    assert op.committed_troops == 5000 and s.cities["성도"].food == 0
    assert any("[병량] 작전" in h and "탈영" in h for h in s.history)


def test_stranded_op_starves():
    # ⭐고립(복귀지 없음)=보급 두절 → 매턴 STRANDED_DESERTION 비율 탈영(말라죽음, 사용자 (b) 확정)
    s = GameState(
        cities={"장안": City(name="장안", owner="위", troops=0, size=1)},
        factions={"위": Faction(name="위", ruler="조조")},
        distances={"장안": {}},
    )
    s.operations.append(ActiveOperation(
        id=1, faction="촉", stage="이동", threshold=5, committed_troops=1000,
        action=Battle(kind="전투", mode="공성", origin="장안", target="장안", troops=1000)))
    _economy_tick(s)
    assert s.operations[0].committed_troops == 1000 - round(1000 * STRANDED_DESERTION)
    assert any("고립" in h and "탈영" in h for h in s.history)


def test_food_runway_alert_and_surplus():
    # ⭐군량 경보: 현 소모율 기준 고갈까지 개월 수(결정론). 흑자=None.
    s = _state(size=1, troops=40000, food=1200)   # 수입 500 − 주둔 1000 = −500/월 → 1200/500 = 2개월
    assert food_runway(s, "성도") == 2
    s2 = _state(size=1, troops=10000, food=0)     # 주둔 250 < 수입 500 → 흑자
    assert food_runway(s2, "성도") is None
