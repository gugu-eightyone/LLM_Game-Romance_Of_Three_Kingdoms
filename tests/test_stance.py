"""태세(출성) + 성벽 정원 커플링 테스트 (§9-20). 전부 오프라인·결정론.

경계값 중심: 빈 성벽 무위력·부분 정원·정상 케이스 무변 / 출성 성립·기각·감속·복귀.
"""
from src.config import SIEGE_RETREAT_SURVIVAL, SORTIE_SLOW_CAP, WALL_DEFENSE, WALL_MANNING
from src.models import ActiveOperation, Battle, City, Faction, GameState, General
from src.engine import Force, _capture_city, _combat_round, advance_turn, start_operation


def _mini_state() -> GameState:
    """위(업 20000)가 촉 한중(수비 8000·벽2)을 노리는 국면. 업↔한중 인접(거리1)."""
    return GameState(
        cities={
            "한중": City(name="한중", owner="촉", troops=8000, food=5000, gold=3000,
                          wall=2, generals=["위연"]),
            "성도": City(name="성도", owner="촉", troops=10000, food=5000, gold=5000, wall=2),
            "업":   City(name="업", owner="위", troops=20000, food=9000, gold=9000, wall=2,
                          generals=["장료"]),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={
            "위연": General(name="위연", command=85),
            "장료": General(name="장료", command=93),
        },
        distances={"한중": {"업": 1, "성도": 1}, "성도": {"한중": 1}, "업": {"한중": 1}},
    )


# ---------- 성벽 정원(wall-병력 커플링) ----------
def test_wall_bonus_capped_by_manning():
    s = _mini_state()
    # 빈 성: 병력 0이면 성벽 보너스도 0 (빈 성벽 혼자 못 싸움)
    _, _, dom_empty = _combat_round(s, Force(10000, []), Force(0, [], wall=3))
    assert dom_empty == 999.0                        # 수비 전투력 완전 0
    # 부분 정원: 병력 500·벽3 → min(3, 0.5)=0.5레벨만 위력
    ap = 10000
    _, _, dom_partial = _combat_round(s, Force(ap, []), Force(500, [], wall=3))
    expected_bp = 500 + 0.5 * WALL_DEFENSE
    assert abs((ap / expected_bp - 1) - dom_partial) < 1e-9
    # 정상 케이스: 정원 충족(병력 ≥ wall×MANNING)이면 종전과 동일한 만액 보너스
    _, _, dom_full = _combat_round(s, Force(ap, []), Force(3 * WALL_MANNING, [], wall=3))
    expected_bp_full = 3 * WALL_MANNING + 3 * WALL_DEFENSE
    assert abs((ap / expected_bp_full - 1) - dom_full) < 1e-9


# ---------- 출성 성립/기각 ----------
def _besiege(s: GameState) -> None:
    """위가 한중 공성 → 1턴 진군해 성 앞 교전 개시."""
    start_operation(s, Battle(kind="전투", mode="공성", origin="업", target="한중",
                              troops=15000, generals=["장료"]))
    advance_turn(s, [])                              # 거리1 → 도착·공성 개시
    assert s.operations[0].stage == "교전"


def test_sortie_requires_besieger():
    s = _mini_state()
    # 성 앞에 적 없음 → 제자리 야전 = 기각(수성은 자동)
    op = start_operation(s, Battle(kind="전투", mode="야전", origin="한중", target="한중",
                                   troops=3000))
    assert op is None
    assert any("출성" in h and "기각" in h for h in s.history)


def _manual_siege(s: GameState, troops: int = 10000) -> None:
    """성 앞 교전 중인 공성 op를 손으로 심음(진군 라운드 생략 = 수치 통제)."""
    s.operations.append(ActiveOperation(
        id=s.next_op_id, faction="위",
        action=Battle(kind="전투", mode="공성", origin="업", target="한중", troops=troops),
        stage="교전", progress=0, threshold=99, committed_troops=troops, committed_generals=[]))
    s.next_op_id += 1


def test_sortie_engages_and_slows_siege():
    """대조 조건 통제: 잔류 수비대가 같을 때, 출성이 있으면 공성 진행이 덜 나간다.

    (출성은 수비대를 빼가 성의 우세도를 올리는 부작용도 있으므로 — 그게 배분 리스크 —
    순수 감속 효과는 '같은 잔류 수비대' 비교로 격리한다. wall=0으로 커플링 변수도 차단.)
    """
    # 대조군: 수비 5000(출성 없음)
    s0 = _mini_state()
    s0.cities["한중"].troops, s0.cities["한중"].wall = 5000, 0
    s0.cities["한중"].generals = []
    _manual_siege(s0)
    advance_turn(s0, [])
    base_progress = s0.operations[0].progress
    assert base_progress > 0                         # 우세 공성이 실제로 진행됨

    # 실험군: 수비 8000에서 3000 출성 → 잔류 5000 동일 + 성 앞 야전
    s1 = _mini_state()
    s1.cities["한중"].troops, s1.cities["한중"].wall = 8000, 0
    s1.cities["한중"].generals = []
    _manual_siege(s1)
    sortie = start_operation(s1, Battle(kind="전투", mode="야전", origin="한중", target="한중",
                                        troops=3000))
    assert sortie is not None and sortie.stage == "교전"   # 이동 없이 즉시 성 앞 교전
    assert s1.cities["한중"].troops == 5000                # 수비대에서 분할 차감
    advance_turn(s1, [])
    assert any("[야전]" in h for h in s1.history)          # 공성군과 실제 교전 발생
    siege = next(o for o in s1.operations if o.action.mode == "공성")
    assert siege.progress < base_progress            # 출성 견제(감속)+야전 소모로 진행 감소
    assert any("출성 견제" in h for h in s1.history)


def test_sortie_returns_home_after_besieger_gone():
    s = _mini_state()
    _besiege(s)
    siege = s.operations[0]
    start_operation(s, Battle(kind="전투", mode="야전", origin="한중", target="한중",
                              troops=6000, generals=["위연"]))
    # 공성군을 인위로 소멸 직전까지 → 다음 턴 야전에서 전멸 → 출성 부대는 성내 복귀
    siege.committed_troops = 1
    before = s.cities["한중"].troops
    advance_turn(s, [])
    assert all(o.action.mode != "공성" for o in s.operations)   # 공성군 소멸
    advance_turn(s, [])                                          # 출성 부대 자동 복귀
    assert not s.operations
    assert s.cities["한중"].troops > before          # 생존 병력이 성으로 돌아옴
    assert "위연" in s.cities["한중"].generals


# ---------- 함락 시 잔존 수비병 퇴각 ----------
def test_capture_survivors_retreat():
    """성벽 돌파 함락: 잔병×생존율이 인접 아군 도시로 퇴각(증발 아님)."""
    s = _mini_state()
    city = s.cities["한중"]                          # 인접: 업(위)·성도(촉) → 퇴로=성도
    city.troops, city.generals = 4000, []
    op = ActiveOperation(id=1, faction="위",
                         action=Battle(kind="전투", mode="공성", origin="업", target="한중", troops=9000),
                         stage="교전", progress=99, threshold=99,
                         committed_troops=9000, committed_generals=[])
    s.operations.append(op)
    before = s.cities["성도"].troops
    _capture_city(s, op, city)
    assert city.owner == "위" and city.troops == 9000
    assert s.cities["성도"].troops == before + round(4000 * SIEGE_RETREAT_SURVIVAL)
    assert any("잔병" in h and "퇴각" in h for h in s.history)


def test_capture_no_survivors_when_encircled():
    """완전 포위(인접 아군 도시 0) → 퇴로 없음 → 잔병 전멸."""
    s = _mini_state()
    s.cities["성도"].owner = "위"                    # 촉 퇴로 봉쇄
    city = s.cities["한중"]
    city.troops, city.generals = 4000, []
    op = ActiveOperation(id=1, faction="위",
                         action=Battle(kind="전투", mode="공성", origin="업", target="한중", troops=9000),
                         stage="교전", progress=99, threshold=99,
                         committed_troops=9000, committed_generals=[])
    s.operations.append(op)
    troops_before = {n: c.troops for n, c in s.cities.items() if n != "한중"}
    _capture_city(s, op, city)
    assert city.owner == "위"
    assert {n: c.troops for n, c in s.cities.items() if n != "한중"} == troops_before  # 아무 데도 안 감
    assert not any("잔병" in h for h in s.history)


def test_sortie_slow_cap():
    """감속은 캡까지만 — 대군 출성이라도 공성이 완전 정지하지는 않는다(설계: 감속≠정지)."""
    assert 0 < SORTIE_SLOW_CAP < 1
    s = _mini_state()
    _besiege(s)
    siege = s.operations[0]
    siege.committed_troops = 1000                    # 출성(6000) ≫ 공성(1000) → 비율 6 > 캡
    start_operation(s, Battle(kind="전투", mode="야전", origin="한중", target="한중", troops=6000))
    advance_turn(s, [])
    # 캡이 물려도 우세도가 음수면 진행 0일 수 있음 — 캡 로그로 확인(감속률 표기가 캡 값)
    assert any(f"출성 견제 −{SORTIE_SLOW_CAP:.0%}" in h for h in s.history)
