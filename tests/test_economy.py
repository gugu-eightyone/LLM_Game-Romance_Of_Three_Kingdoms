"""경제 틱(매턴 수입·병량 소모·탈영) — 전부 오프라인. ⭐사용자 2026-08-30 "금은 언제 올라?"."""
from src.config import CITY_INCOME_FOOD, CITY_INCOME_GOLD, TROOPS_PER_FOOD
from src.engine import advance_turn, load_scenario
from src.models import City, Faction, GameState


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
    # 식량 = 기존 + 수입 − 병량(2000/20=100)
    assert s.cities["성도"].food == 1000 + 2 * CITY_INCOME_FOOD - 2000 // TROOPS_PER_FOOD


def test_starvation_desertion():
    s = _state(size=1, troops=20000, food=0)
    advance_turn(s, [])
    c = s.cities["성도"]
    # 수입 500 < 병량 1000 → 부족 500×20=10000 탈영, 식량 0으로 소진
    assert c.food == 0 and c.troops == 20000 - (20000 // TROOPS_PER_FOOD - CITY_INCOME_FOOD) * TROOPS_PER_FOOD
    assert any("[병량]" in h and "탈영" in h for h in s.history)


def test_size_zero_no_economy():
    s = _state(size=0, troops=5000, food=0, gold=7)
    advance_turn(s, [])
    c = s.cities["성도"]
    assert (c.gold, c.food, c.troops) == (7, 0, 5000)   # 규모 미설정=경제 없음(기존 테스트 보호막)


def test_scenario_has_sizes():
    s = load_scenario()
    assert all(c.size >= 1 for c in s.cities.values())   # 실 시나리오는 전 도시 규모 큐레이트
