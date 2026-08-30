"""아군 상잔 버그 수정(⭐2026-08-31): 공성 target이 그 사이 아군/동맹이 되면 입성/복귀.

재현 배경: 촉이 장안을 함락한 뒤에도 같은 장안을 노리던 다른 촉 공성 부대가
아군 수비대와 전투 판정을 돌던 실플레이 버그. 출발 가드만으론 소유 변경을 못 봄.
"""
from src.engine import advance_turn
from src.models import ActiveOperation, Battle, City, Faction, GameState


def _state(target_owner="촉", alliances=None):
    return GameState(
        cities={
            "한중": City(name="한중", owner="촉", troops=1000),
            "장안": City(name="장안", owner=target_owner, troops=5000),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        distances={"한중": {"장안": 2}, "장안": {"한중": 2}},
        alliances=alliances or [],
    )


def _siege_op(stage, progress=0.0):
    return ActiveOperation(
        id=1, faction="촉", stage=stage, progress=progress, threshold=2,
        committed_troops=3000,
        action=Battle(kind="전투", mode="공성", origin="한중", target="장안", troops=3000))


def test_siege_on_now_friendly_city_joins():
    """교전 중 target이 아군 — 전투 없이 입성(합류), 병력 무손실."""
    s = _state()
    s.operations.append(_siege_op("교전"))
    advance_turn(s, [])
    assert s.operations == []
    assert s.cities["장안"].troops == 5000 + 3000        # 아군 상잔 0, 그대로 합류
    assert any("입성" in h for h in s.history)


def test_arriving_siege_joins_friendly_city():
    """이동 중 아군이 선점 함락 → 도착 시 공성 개시 대신 입성."""
    s = _state()
    s.operations.append(_siege_op("이동", progress=1.0))  # 이번 턴 +1 → 도착
    advance_turn(s, [])
    assert s.operations == [] and s.cities["장안"].troops == 8000


def test_siege_on_allied_city_returns_home():
    """교전 중 동맹 체결 — 합류(소유권 위반) 아닌 복귀."""
    s = _state(target_owner="위", alliances=[tuple(sorted(("위", "촉")))])
    s.operations.append(_siege_op("교전"))
    advance_turn(s, [])
    assert s.operations == []
    assert s.cities["장안"].troops == 5000               # 동맹 도시 무변(전투도 합류도 없음)
    assert s.cities["한중"].troops == 1000 + 3000        # 출발지로 복귀
