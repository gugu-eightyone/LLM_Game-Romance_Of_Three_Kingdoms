"""사기 이벤트 훅 테스트 — ⭐"올라가기만 하고 내려가는 요인이 없었다" 해소(§9-10 배선). 전부 오프라인."""
from src.config import (
    MORALE_CITY_LOST, MORALE_CITY_TAKEN, MORALE_FEAST_CAP, MORALE_RULER_CAPTURED,
)
from src.engine import advance_turn, apply_domestic, _take_prisoner
from src.models import Battle, City, Domestic, Faction, GameState, General


def _state() -> GameState:
    """촉 vs 위(도시 2개=함락돼도 생존). 사기 기본 50."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=15000, gold=50000, generals=["조운"]),
            "업":   City(name="업", owner="위", troops=500),
            "허창": City(name="허창", owner="위", troops=8000),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91, faction="촉"),
                  "조조": General(name="조조", command=96, is_ruler=True, faction="위")},
        distances={"성도": {"업": 1}, "업": {"성도": 1, "허창": 1}, "허창": {"업": 1}},
    )


def test_capture_raises_winner_and_lowers_loser():
    s = _state()
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="업",
                            troops=15000, generals=["조운"])])
    for _ in range(6):
        if s.cities["업"].owner == "촉":
            break
        advance_turn(s, [])
    assert s.cities["업"].owner == "촉"
    assert s.factions["촉"].morale == 50 + MORALE_CITY_TAKEN
    assert s.factions["위"].morale == 50 + MORALE_CITY_LOST


def test_ruler_capture_hits_faction_morale_hard():
    s = _state()
    _take_prisoner(s, "성도", "조조")
    assert s.factions["위"].morale == 50 + MORALE_RULER_CAPTURED


def test_repelled_siege_does_not_touch_morale():
    """⭐사기=세력 정신적 무장 — 국지 전투(공성 격퇴) 승패는 사기 판정에서 제외."""
    s = _state()
    s.cities["업"].troops = 20000                      # 수비 압도 → 공성군 소진=격퇴
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="업",
                            troops=600, generals=[])])
    for _ in range(10):
        if not s.operations:
            break
        advance_turn(s, [])
    assert any("격퇴" in h for h in s.history)
    assert s.factions["촉"].morale == 50               # 무변


def test_feast_cannot_push_morale_above_cap():
    """⭐잔치 천장: 금 도배로 시작부터 사기 100 만들기 방지(그 위는 승리로만)."""
    s = _state()
    apply_domestic(s, Domestic(kind="내정", city="성도", item="사기진작", gold_spent=50000), actor="촉")
    assert s.factions["촉"].morale == MORALE_FEAST_CAP
    s.factions["촉"].morale = 85                       # 이미 천장 위(승리로 획득) → 잔치 무효, 하락도 없음
    apply_domestic(s, Domestic(kind="내정", city="성도", item="사기진작", gold_spent=1000), actor="촉")
    assert s.factions["촉"].morale == 85
