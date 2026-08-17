"""규칙 엔진 단위 테스트 (증분1). 결정론이라 고정 입력 → 고정 결과.

경계값 중심: 과투입 클램프·비인접 기각·이동→교전 전환·함락 자원보존·격퇴·참수 게이트·내정.
"""
from src.models import Battle, City, Domestic, Faction, GameState, General
from src.engine import (
    _combat_round, _power, advance_turn, apply_domestic,
    check_victory, start_operation,
)


def _mini_state() -> GameState:
    """작은 통제 상태: 촉(성도·한중) vs 위(업). 성도↔업 인접(거리1)."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=10000, food=5000, gold=5000,
                          wall=1, generals=["조운"]),
            "한중": City(name="한중", owner="촉", troops=5000, food=3000, gold=2000, wall=1),
            "업":   City(name="업", owner="위", troops=3000, food=2000, gold=4000, wall=0,
                          generals=["조조"]),
        },
        factions={
            "촉": Faction(name="촉", ruler="유비"),
            "위": Faction(name="위", ruler="조조"),
        },
        generals={
            "조운": General(name="조운", command=91, might=95, intel=76),
            "조조": General(name="조조", command=96, might=72, intel=91, is_ruler=True),
        },
        distances={"성도": {"업": 1, "한중": 1}, "한중": {"성도": 1}, "업": {"성도": 1}},
    )


def test_power_monotonic():
    s = _mini_state()
    # 병력 많을수록 강함
    assert _power(20000, [], s) > _power(10000, [], s)
    # 장수(통솔 91)가 붙으면 보정으로 더 강함
    assert _power(10000, ["조운"], s) > _power(10000, [], s)
    # 로스터에 없는 이름은 보정 0(환각 무시)
    assert _power(10000, ["없는장수"], s) == _power(10000, [], s)


def test_power_uses_top_commander():
    """지휘관(최고 통솔) 1명만 반영 — 장수 수 스택 무보상(부장 시스템 없음)."""
    s = _mini_state()
    s.generals["장비"] = General(name="장비", command=82)   # 조운 command 91
    # 조운(91)+장비(82) = 조운 단독과 동일(최고통솔만 반영)
    assert _power(10000, ["조운", "장비"], s) == _power(10000, ["조운"], s)
    # 통솔 낮은 장수 단독은 더 약함
    assert _power(10000, ["장비"], s) < _power(10000, ["조운"], s)


def test_start_operation_clamps_overcommit():
    s = _mini_state()
    op = start_operation(s, Battle(kind="전투", mode="공성", origin="성도", target="업",
                                   troops=99999, generals=["조운"]))
    assert op is not None
    assert op.committed_troops == 10000            # 보유 상한으로 클램프
    assert s.cities["성도"].troops == 0            # 차감 정확
    assert "조운" not in s.cities["성도"].generals  # 장수 행군해 나감
    assert any("환각" in h for h in s.history)      # 위반 로깅(A층 카운트 표면)


def test_start_operation_rejects_nonadjacent():
    s = _mini_state()
    # 성도→한중은 인접이나, 한중→업은 distances에 없음(비인접) → 순간이동 기각
    op = start_operation(s, Battle(kind="전투", mode="공성", origin="한중", target="업",
                                   troops=1000))
    assert op is None
    assert len(s.operations) == 0
    assert any("비인접" in h for h in s.history)


def test_march_then_siege_transition():
    s = _mini_state()
    start_operation(s, Battle(kind="전투", mode="공성", origin="성도", target="업", troops=8000))
    op = s.operations[0]
    assert op.stage == "이동"
    advance_turn(s, [])                            # 거리1 → 1개월 진군 → 도착
    assert op.stage == "교전"


def test_capture_transfers_and_conserves_resources():
    s = _mini_state()
    gold_before = sum(c.gold for c in s.cities.values())
    # 압도적 병력 → 한 라운드 교전에 수비 병력 소멸 → 함락
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="업",
                            troops=10000, generals=["조운"])])   # 개시+진군(도착)
    advance_turn(s, [])                                          # 교전 → 함락
    assert s.cities["업"].owner == "촉"                          # 소유 이전
    assert "조운" in s.cities["업"].generals                    # 공격 장수 접수
    assert sum(c.gold for c in s.cities.values()) == gold_before  # 금 보존(약탈=이전, 복사 X)


def test_repel_when_attacker_collapses():
    s = _mini_state()
    # 약한 공격(500) vs 업 3000 → 공격측 붕괴 → 격퇴, 소유 불변
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="업", troops=500)])
    for _ in range(6):
        advance_turn(s, [])
    assert s.cities["업"].owner == "위"
    assert len(s.operations) == 0
    assert any("격퇴" in h for h in s.history)


def test_last_city_captures_ruler_and_wins():
    s = _mini_state()
    # 위 마지막 거점(업) 함락 → 탈출로 0(완전 포위) → 조조 포획·위 멸망 → 천하통일
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="업",
                            troops=10000, generals=["조운"])])
    advance_turn(s, [])
    assert s.factions["위"].alive is False
    assert "조조" in s.cities["업"].prisoners        # 포로=도시 수감(포획자=승자)
    check_victory(s)
    assert s.winner == "촉"                          # 전 도시 촉 = 천하통일


def test_defender_escapes_or_captured_not_vanish():
    """탈출로(아군 인접 B) 열림 → 방어 장수는 증발 X: 포로(A) 또는 탈출(B). 포위도 0.5→포획 0.25."""
    s = GameState(
        cities={
            "A": City(name="A", owner="위", troops=2000, wall=0, generals=["장료"]),
            "B": City(name="B", owner="위", troops=3000, wall=0),               # 위 아군 = 탈출로
            "C": City(name="C", owner="촉", troops=20000, generals=["조운"]),    # 공격 출발
        },
        factions={"위": Faction(name="위", ruler="장료"), "촉": Faction(name="촉", ruler="유비")},
        generals={"장료": General(name="장료", command=95), "조운": General(name="조운", command=91)},
        distances={"A": {"B": 1, "C": 1}, "B": {"A": 1}, "C": {"A": 1}},
        seed=1,
    )
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="C", target="A",
                            troops=20000, generals=["조운"])])
    advance_turn(s, [])
    assert s.cities["A"].owner == "촉"
    assert ("장료" in s.cities["A"].prisoners) or ("장료" in s.cities["B"].generals)  # 증발 아님
    assert "장료" not in s.cities["A"].generals      # 접수 후 A 주둔 = 공격군만


def test_domestic_recruit_deducts_gold():
    s = _mini_state()
    apply_domestic(s, Domestic(kind="내정", city="성도", item="모병", gold_spent=1000))
    assert s.cities["성도"].troops == 10000 + 1000 * 2   # 금 1당 병력 2
    assert s.cities["성도"].gold == 5000 - 1000


def test_domestic_wall_and_overspend_clamp():
    s = _mini_state()
    # 보유(2000) 초과 지출 요청 → 보유만큼만 차감
    apply_domestic(s, Domestic(kind="내정", city="한중", item="성벽보수", gold_spent=999999))
    assert s.cities["한중"].gold == 0
    assert s.cities["한중"].wall > 1                     # 성벽 상승


def test_morale_hard_clamp_at_100():
    s = _mini_state()
    s.factions["촉"].morale = 98
    apply_domestic(s, Domestic(kind="내정", city="성도", item="사기진작", gold_spent=5000))
    assert s.factions["촉"].morale == 100               # 사기 0~100 하드 바운드


def test_combat_round_wall_helps_defender():
    s = _mini_state()
    _, _, dom_nowall = _combat_round(10000, [], 10000, [], s, wall=0)
    _, _, dom_wall = _combat_round(10000, [], 10000, [], s, wall=3)
    assert dom_nowall > dom_wall                        # 성벽이 방어 우세도 낮춤
