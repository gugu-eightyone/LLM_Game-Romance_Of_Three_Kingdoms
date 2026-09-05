"""D묶음(2026-09-05): 길목 대기 · 개인 이동 · 왕복 호송 · 회군 무료.

설계 근거: docs/LLM_Game_TODO.md §D묶음·마찰 23·26. 전부 오프라인(LLM 0).
"""
from src.engine import _field_engagements, advance_turn, start_travel, travel_path
from src.models import (ActiveOperation, Battle, City, Domestic, Faction,
                        GameState, OpCommand, Transfer, Travel)


def _state(**owners):
    """한중(촉)–장안(위) 거리2 기본판. owners로 소유 덮어쓰기."""
    s = GameState(
        cities={
            "한중": City(name="한중", owner=owners.get("한중", "촉"), troops=20000, generals=["마초"]),
            "장안": City(name="장안", owner=owners.get("장안", "위"), troops=20000),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        distances={"한중": {"장안": 2}, "장안": {"한중": 2}},
    )
    return s


# ======================= 길목 대기 =======================
def test_hold_freezes_at_point():
    s = _state()
    advance_turn(s, {"촉": [Battle(kind="전투", mode="야전", origin="한중", target="장안",
                                  troops=8000, hold_at=1)]})
    op = s.operations[0]
    assert op.progress == 1.0 and any("길목 대기" in h for h in s.history)
    for _ in range(3):                                # 대기=동결(도착·복귀 없음)
        advance_turn(s, [])
    assert s.operations == [op] and op.progress == 1.0 and op.stage == "이동"


def test_hold_intercepts_passing_enemy_and_stays():
    s = _state()
    s.operations.append(ActiveOperation(                # 촉: 1개월 지점 대기 중
        id=1, faction="촉", stage="이동", progress=1.0, threshold=2, committed_troops=9000,
        action=Battle(kind="전투", mode="야전", origin="한중", target="장안", troops=9000, hold_at=1)))
    s.operations.append(ActiveOperation(                # 위: 한중 공성 진군(지나가야 함)
        id=2, faction="위", stage="이동", progress=0.0, threshold=2, committed_troops=500,
        action=Battle(kind="전투", mode="공성", origin="장안", target="한중", troops=500)))
    s.next_op_id = 3
    advance_turn(s, [])                                 # 위 +1 → 진행합 2 ≥ 거리 2 → 요격
    assert any("[야전]" in h for h in s.history)
    assert not any(o.id == 2 for o in s.operations)     # 소부대 궤멸
    hold_op = next(o for o in s.operations if o.id == 1)
    assert hold_op.progress == 1.0 and hold_op.has_fought   # ⭐요격 후에도 지점 사수(자동 복귀 없음)
    advance_turn(s, [])
    assert any(o.id == 1 for o in s.operations)


def test_hold_rejected_for_siege_and_short_road():
    s = _state()
    advance_turn(s, {"촉": [Battle(kind="전투", mode="공성", origin="한중", target="장안",
                                  troops=5000, hold_at=1)]})
    assert any("길목 대기는 거리 2+ 야전만" in h for h in s.history)
    assert s.operations[0].action.hold_at == 0          # 무시하고 정상 진군


def test_hold_clamped_into_range():
    s = _state()
    advance_turn(s, {"촉": [Battle(kind="전투", mode="야전", origin="한중", target="장안",
                                  troops=5000, hold_at=7)]})
    assert s.operations[0].action.hold_at == 1          # 거리2 → 지점은 1뿐
    assert any("클램프" in h for h in s.history)


# ======================= 회군 무료 (마찰 23) =======================
def test_withdraw_is_free_of_order_cap():
    s = _state()
    s.operations.append(ActiveOperation(
        id=1, faction="촉", stage="이동", progress=0.5, threshold=2, committed_troops=3000,
        action=Battle(kind="전투", mode="야전", origin="한중", target="장안", troops=3000)))
    s.next_op_id = 2
    acts = [OpCommand(kind="작전지시", op_id=1, order="회군")] \
        + [Domestic(kind="내정", city="한중", item="식량증산", gold_spent=0) for _ in range(5)]
    advance_turn(s, {"촉": acts})                       # 회군 1(무료) + 내정 5 → 내정만 4로 트림
    assert sum(1 for h in s.history if "[내정]" in h) == 4
    assert any("회군" in h for h in s.history)          # 회군은 상한 밖에서 실행됨
    assert any("상한" in h and "회군 제외" in h for h in s.history)


# ======================= 개인 이동 (26①) =======================
def _chain(mid_owner="촉"):
    """성도–한중–장안 사슬(거리 1+2). 성도에 조운."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=1000, generals=["조운"]),
            "한중": City(name="한중", owner=mid_owner, troops=1000),
            "장안": City(name="장안", owner="촉", troops=1000),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        distances={"성도": {"한중": 1}, "한중": {"성도": 1, "장안": 2}, "장안": {"한중": 2}},
    )


def test_travel_multi_hop_arrives():
    s = _chain()
    assert travel_path(s, "촉", "성도", "장안") == (["성도", "한중", "장안"], 3)
    op = start_travel(s, Travel(kind="개인이동", origin="성도", target="장안", general="조운"), actor="촉")
    assert op is not None and "조운" not in s.cities["성도"].generals
    for _ in range(3):
        advance_turn(s, [])
    assert s.operations == [] and "조운" in s.cities["장안"].generals


def test_travel_rejects_enemy_corridor():
    s = _chain(mid_owner="위")                          # 경유지가 적 영토 → 잠입 기각
    op = start_travel(s, Travel(kind="개인이동", origin="성도", target="장안", general="조운"), actor="촉")
    assert op is None and any("아군 영토 경로 없음" in h for h in s.history)
    assert "조운" in s.cities["성도"].generals          # 출발도 안 함(무손실)


def test_travel_immune_to_interception():
    s = _state()
    s.cities["장안"].owner = "촉"                       # 목적지도 아군이어야 출발 성립
    op = start_travel(s, Travel(kind="개인이동", origin="한중", target="장안", general="마초"), actor="촉")
    s.operations.append(ActiveOperation(                # 같은 간선 반대방향 적 부대(요격 술어 충족 상황)
        id=99, faction="위", stage="이동", progress=1.5, threshold=2, committed_troops=9000,
        action=Battle(kind="전투", mode="야전", origin="장안", target="한중", troops=9000)))
    assert all(op not in pair for pair in _field_engagements(s))   # ⭐완전 면제
    advance_turn(s, [])
    assert not any("[야전]" in h for h in s.history)
    assert any(o.id == op.id for o in s.operations)     # 무사 통과 중


def test_travel_returns_safely_when_target_falls():
    s = _chain()
    start_travel(s, Travel(kind="개인이동", origin="성도", target="장안", general="조운"), actor="촉")
    s.cities["장안"].owner = "위"                       # 이동 중 목적지 함락
    for _ in range(4):
        advance_turn(s, [])
    assert "조운" in s.cities["성도"].generals          # 무사 회군(리스크 0)
    assert any("개인 이동 취소(목적지 상실)" in h for h in s.history)


# ======================= 왕복 호송 (26②) =======================
def test_round_trip_unloads_and_escort_returns():
    s = _state()
    s.cities["장안"].owner = "촉"
    advance_turn(s, {"촉": [Transfer(kind="호송", origin="한중", target="장안",
                                    troops=500, generals=["마초"], round_trip=True)]})
    advance_turn(s, [])                                 # 거리2 → 2턴째 도착·하역, 복귀 개시
    assert s.cities["장안"].troops == 20000 + 500       # 병력=하역
    assert "마초" not in s.cities["장안"].generals      # ⭐장수는 안 내림(왕복)
    back = s.operations[0]
    assert back.action.origin == "장안" and back.action.target == "한중"
    assert back.committed_troops == 0 and not back.action.round_trip   # 빈 몸·편도
    advance_turn(s, [])
    advance_turn(s, [])
    assert s.operations == [] and "마초" in s.cities["한중"].generals  # 호위 장수 귀환


def test_round_trip_without_general_is_one_way():
    s = _state()
    s.cities["장안"].owner = "촉"
    advance_turn(s, {"촉": [Transfer(kind="호송", origin="한중", target="장안",
                                    troops=500, round_trip=True)]})
    advance_turn(s, [])
    assert s.operations == []                           # 복귀할 호위 장수가 없으면 편도로 해소
    assert s.cities["장안"].troops == 20500
