"""배치 1(2026-09-01 전수조사) — 소유 변경·세력 소멸 경계 수정 테스트. 전부 오프라인.

커버: 성벽보수 공짜 exploit / 탈환·항복 시 자국 포로 해방 / 군주 2지선다 / 승계=세력 소속 기준 /
좀비 세력(잔존 부대 투항) / 야전 전리품=격멸자 귀속 / 외교 큐 크래시·죽은 세력 동맹 / 항복 게이트(도시 기준).
"""
from src.engine import (
    _destroy_field_op, _take_prisoner, advance_turn, apply_diplomacy, apply_disposition,
    apply_domestic, surrender_gate,
)
from src.models import (
    ActiveOperation, Battle, City, Diplomacy, Domestic, Faction, GameState, General, Proposal,
)


def _state(seed: int = 0) -> GameState:
    """위·촉·오 삼각 인접(외교 테스트 fixture와 동형)."""
    return GameState(
        seed=seed,
        cities={
            "낙양": City(name="낙양", owner="위", troops=20000, gold=5000, food=8000, generals=["장료"]),
            "성도": City(name="성도", owner="촉", troops=15000, gold=3000, food=6000, generals=["조운"]),
            "건업": City(name="건업", owner="오", troops=15000, gold=4000, food=5000, generals=["여몽"]),
        },
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비"),
                  "오": Faction(name="오", ruler="손권")},
        generals={"장료": General(name="장료", command=95, intel=70, faction="위"),
                  "조운": General(name="조운", command=91, intel=76, faction="촉"),
                  "여몽": General(name="여몽", command=90, intel=88, faction="오")},
        distances={"낙양": {"성도": 1, "건업": 2}, "성도": {"낙양": 1, "건업": 1},
                   "건업": {"낙양": 2, "성도": 1}},
    )


# ---------- 성벽보수: 공짜 exploit 차단 + HP 복구 의미론(⭐HP화) ----------
def test_wall_repair_requires_damage_and_gold():
    s = _state()
    c = s.cities["낙양"]                                 # wall 0 → 최대 HP 2000(SIEGE_BASE 몫)
    apply_domestic(s, Domestic(kind="내정", city="낙양", item="성벽보수", gold_spent=0))
    assert c.wall == 0 and c.gold == 5000                # 온전=기각·금 미소모(공짜 +1 없음)
    assert any("[기각]" in h and "성벽보수" in h for h in s.history)
    c.wall_hp = 500                                      # 파손 1500
    apply_domestic(s, Domestic(kind="내정", city="낙양", item="성벽보수", gold_spent=1))
    assert c.wall_hp == 500                              # 금 3 미만 → HP 0 복구 = 기각
    apply_domestic(s, Domestic(kind="내정", city="낙양", item="성벽보수", gold_spent=99999))
    assert c.wall_hp == 2000                             # 보유 5000 → heal=min(1500, 1666)=1500
    assert c.gold == 5000 - 1500 * 3                     # 파손분만 과금


def test_wall_damage_persists_across_capture():
    """부서진 성벽은 새 주인이 그대로 물려받는다(구 '진행도 승계' 구멍의 물리적 해소)."""
    from src.engine import _capture_city
    from src.models import ActiveOperation
    s = _state()
    c = s.cities["낙양"]
    c.wall_hp = 700
    op = ActiveOperation(id=3, faction="촉", stage="교전", threshold=0, committed_troops=9000,
                         committed_generals=[],
                         action=Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=9000))
    s.operations.append(op)
    _capture_city(s, op, c)
    assert c.owner == "촉" and c.wall_hp == 700          # 손상은 도시 소유 — 함락로도 리셋 없음


def test_wall_regen_peacetime_only():
    s = _state()
    s.cities["낙양"].wall_hp = 1000
    advance_turn(s, [])                                  # 평시 → +200
    assert s.cities["낙양"].wall_hp == 1200
    s2 = _state()
    s2.cities["낙양"].wall_hp = 1000
    advance_turn(s2, [Battle(kind="전투", mode="공성", origin="건업", target="낙양",
                             troops=5000, generals=[])])  # 오 접근 중(거리2, 이동) = 피침
    assert s2.cities["낙양"].wall_hp == 1000             # 위협 중 회복 없음


def test_capture_emergency_wall_heal():
    """⭐점령 턴 말 응급 보수: 주인 바뀐 성 = 최대 HP의 1/5 회복(그 턴 자연회복은 제외 — 정확히 1/5만)."""
    s = _state()
    s.cities["낙양"].troops = 100
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                            troops=12000, generals=[])])
    for _ in range(4):
        if s.cities["낙양"].owner == "촉":
            break
        advance_turn(s, [])
    assert s.cities["낙양"].owner == "촉"
    assert s.cities["낙양"].wall_hp == round(2000 * 0.2)   # 돌파(0) + 응급 보수 400, 자연회복 미중복


def test_breached_wall_held_by_dominant_garrison():
    """HP 0(돌파 상태)이어도 수비가 우세하면 함락 아님 — 약체 공성의 날먹 방지."""
    from src.models import ActiveOperation
    s = _state()
    c = s.cities["낙양"]
    c.wall_hp, c.troops = 0, 20000
    s.operations.append(ActiveOperation(
        id=4, faction="촉", stage="교전", threshold=0, committed_troops=500, committed_generals=[],
        action=Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=500)))
    advance_turn(s, [])
    assert s.cities["낙양"].owner == "위"                # 우세도 음수 → 무너진 성도 지켜냄


# ---------- 탈환 시 자국 포로 해방 ----------
def test_recapture_liberates_own_prisoners():
    s = _state()
    s.cities["성도"].generals.remove("조운")
    s.cities["낙양"].prisoners.append("조운")          # 촉 조운이 위 낙양에 수감
    s.pending_captives.append(("낙양", "조운"))
    s.cities["낙양"].troops = 100
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                            troops=12000, generals=[])])
    for _ in range(6):
        if s.cities["낙양"].owner == "촉":
            break
        advance_turn(s, [])
    assert s.cities["낙양"].owner == "촉"
    assert "조운" in s.cities["낙양"].generals          # 해방=즉시 합류
    assert "조운" not in s.cities["낙양"].prisoners
    assert ("낙양", "조운") not in s.pending_captives
    assert any("해방" in h and "조운" in h for h in s.history)


# ---------- 군주 처분 = 석방/처형 2지선다 ----------
def test_ruler_imprison_rejected_release_ok():
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "낙양", "손권")
    assert apply_disposition(s, "낙양", "손권", "수감") is False
    assert "손권" in s.cities["낙양"].prisoners         # 잔존(다음 턴 재질의 대상)
    assert any("[환각]" in h and "수감 불가" in h for h in s.history)
    assert apply_disposition(s, "낙양", "손권", "석방") is True


def test_fallen_ruler_can_be_imprisoned():
    """망국 군주=일반 장수 취급(§9-21⑤ 정합) — 2지선다 게이트는 세력 생존 시에만."""
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    s.factions["오"].alive = False
    _take_prisoner(s, "낙양", "손권")
    assert apply_disposition(s, "낙양", "손권", "수감") is True


# ---------- 승계 = 세력 소속 기준(출전 중 포함) ----------
def test_succession_includes_field_generals():
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    s.cities["건업"].generals.remove("여몽")           # 여몽은 야전 출전 중(도시 비주둔)
    s.operations.append(ActiveOperation(
        id=99, faction="오", stage="이동", threshold=5, committed_troops=1000,
        committed_generals=["여몽"],
        action=Battle(kind="전투", mode="야전", origin="건업", target="낙양", troops=1000)))
    _take_prisoner(s, "낙양", "손권")
    apply_disposition(s, "낙양", "손권", "처형")
    assert s.factions["오"].ruler == "여몽" and s.generals["여몽"].is_ruler


# ---------- 좀비 세력: 멸망 시 잔존 부대 투항 ----------
def test_extinct_faction_remnants_defect_to_conqueror():
    s = _state()
    s.cities["건업"].troops = 100
    s.cities["건업"].generals = []
    s.operations.append(ActiveOperation(                # 오의 야전 부대가 밖에 나가 있음
        id=7, faction="오", stage="이동", threshold=5, committed_troops=5000,
        committed_generals=["여몽"],
        action=Battle(kind="전투", mode="야전", origin="건업", target="낙양", troops=5000)))
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="건업",
                            troops=12000, generals=[])])
    for _ in range(6):
        if not s.factions["오"].alive:
            break
        advance_turn(s, [])
    assert not s.factions["오"].alive
    assert not any(o.faction == "오" for o in s.operations)   # 좀비 부대 없음
    assert "여몽" in s.cities["건업"].generals                # 함락 도시로 흡수
    assert s.generals["여몽"].faction == "촉"                 # 투항=정복 세력 소속
    assert any("투항" in h for h in s.history)


# ---------- 야전 전리품 = 격멸자 귀속 ----------
def test_field_spoils_go_to_killer_not_nearest():
    s = _state()
    s.cities["낙양"].generals.remove("장료")
    op = ActiveOperation(                               # 위 부대, 촉 방면(성도)으로 가다 오에게 궤멸된 상황
        id=9, faction="위", stage="이동", threshold=1, committed_troops=0,
        committed_generals=["장료"], cargo_gold=500,
        action=Battle(kind="전투", mode="야전", origin="낙양", target="성도", troops=0))
    s.operations.append(op)
    _destroy_field_op(s, op, n_field=7, killer="오")    # prob=min(1, .15×7)=1 → 확정 포획
    assert "장료" in s.cities["건업"].prisoners          # 구 규칙이면 최근접 성도(촉)행이었음
    assert s.cities["건업"].gold == 4000 + 500          # 노획도 격멸자에게


# ---------- 외교 큐: 크래시·죽은 세력 동맹 ----------
def test_surrender_mid_queue_no_crash_no_dead_alliance(monkeypatch):
    """항복 수락이 큐를 필터링해도 ValueError 없이 완주 + 죽은 세력 발신 제안 폐기."""
    import src.decide as decide
    s = _state()
    s.cities["건업"].troops = 1000
    s.proposals = [
        Proposal(from_faction="위", to_faction="오", proposal="항복권유"),
        Proposal(from_faction="촉", to_faction="오", proposal="동맹"),
        Proposal(from_faction="오", to_faction="촉", proposal="동맹"),
    ]
    monkeypatch.setattr(decide, "structured_complete",
                        lambda fmt, sys, usr, **kw: decide.ProposalResponse(accept=True, reason="수락"))
    decide.resolve_proposals(s)                         # 수정 전엔 여기서 ValueError
    assert not s.factions["오"].alive
    assert all("오" not in pair for pair in s.alliances)  # 죽은 세력과 동맹 없음
    assert s.proposals == []


# ---------- 경미 잔건 일괄(2026-09-02) ----------
def test_isolated_destroy_no_silent_vanish():
    """고립(복귀지 없음) 전멸 시 장수=적성 감옥 확정 포로 — 무로그 증발 금지."""
    s = _state(seed=3)
    s.cities["낙양"].owner = "오"                      # 위=무도시(고립 확정)
    s.cities["낙양"].generals.remove("장료")
    op = ActiveOperation(id=9, faction="위", stage="이동", threshold=1, committed_troops=0,
                         committed_generals=["장료"],
                         action=Battle(kind="전투", mode="야전", origin="낙양", target="성도", troops=0))
    s.operations.append(op)
    _destroy_field_op(s, op, n_field=1, killer="오")   # 확률 15% — 실패해도 고립 포로로 수렴
    held = any("장료" in c.prisoners for c in s.cities.values())
    assert held and any("장료" in h for h in s.history)


def test_strand_general_never_vanishes():
    """⭐무증발 원칙: 복귀지 없는 장수 = 아군 아무 도시 구사일생, 아군 전무 = 아무 적성 도시 포로."""
    from src.engine import _strand_general
    s = _state()
    op = ActiveOperation(id=11, faction="위", stage="이동", threshold=1, committed_troops=0,
                         committed_generals=[],
                         action=Battle(kind="전투", mode="야전", origin="성도", target="건업", troops=0))
    _strand_general(s, op, "장료")
    assert "장료" in s.cities["낙양"].generals          # 아군 도시가 있으면 구사일생 귀환
    s2 = _state()
    s2.cities["낙양"].owner = "오"                     # 위=무도시
    _strand_general(s2, op, "장료")
    assert any("장료" in c.prisoners for c in s2.cities.values())   # 최후엔 포로 — 증발은 없다


def test_isolated_return_releases_cargo_prisoners():
    """고립 복귀 실패: 화물 포로=해방(전멸 경로와 일관), 금·식량·장수 소실은 로깅."""
    from src.engine import _return_home
    s = _state()
    s.cities["낙양"].owner = "오"                      # 위=무도시
    s.cities["성도"].generals.remove("조운")
    op = ActiveOperation(id=8, faction="위", stage="이동", threshold=1, committed_troops=200,
                         committed_generals=[], cargo_gold=300, cargo_prisoners=["조운"],
                         action=Battle(kind="전투", mode="야전", origin="낙양", target="성도", troops=200))
    s.operations.append(op)
    _return_home(s, op, "격퇴")
    assert "조운" in s.cities["성도"].generals          # 포로 해방 → 원 세력(촉) 최근접
    assert any("고립 소실" in h and "금300" in h for h in s.history)
    assert any("해산" in h and "200" in h for h in s.history)   # 위=무도시 → 병사 해산(무로그 증발 금지)


def test_isolated_troops_survive_to_far_city():
    """⭐고립 귀환 병사(사용자 지적): 먼 아군 도시가 있으면 절반 생환(SIEGE_RETREAT_SURVIVAL 재사용)."""
    from src.engine import _return_home
    s = GameState(
        cities={"X": City(name="X", owner="오", troops=100), "Y": City(name="Y", owner="오", troops=100),
                "Z": City(name="Z", owner="위", troops=100)},   # Z=위의 먼 도시(간선 없음=인접권 밖)
        factions={"위": Faction(name="위", ruler="조조"), "오": Faction(name="오", ruler="손권")},
        generals={},
        distances={"X": {"Y": 1}, "Y": {"X": 1}},
    )
    op = ActiveOperation(id=12, faction="위", stage="이동", threshold=1, committed_troops=200,
                         committed_generals=[],
                         action=Battle(kind="전투", mode="야전", origin="X", target="Y", troops=200))
    s.operations.append(op)
    _return_home(s, op, "격퇴")
    assert s.cities["Z"].troops == 100 + 100            # 절반 생환
    assert any("구사일생 귀환" in h for h in s.history)


def test_ghost_convoy_does_not_end_interceptor():
    """병력 0 호송(장수 단독)을 잡은 요격대는 '임무 완료' 귀환하지 않는다(유령 교전 방지)."""
    from src.models import Transfer
    s = GameState(
        seed=0,
        cities={"A": City(name="A", owner="촉", troops=9000, generals=["조운"]),
                "B": City(name="B", owner="촉", troops=100),
                "C": City(name="C", owner="위", troops=9000)},
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91, faction="촉")},
        distances={"A": {"B": 3}, "B": {"A": 3, "C": 1}, "C": {"B": 1}},
    )
    advance_turn(s, [Transfer(kind="호송", origin="A", target="B", generals=["조운"])])
    s.cities["B"].owner = "위"                         # 목적지가 전선화 → 위 요격대가 같은 간선 역주행
    advance_turn(s, [Battle(kind="전투", mode="야전", origin="B", target="A", troops=8000)])
    # 2턴째 이동에서 progress 합(2+1)=3 ≥ 거리 3 → 교차 → 호송(병0) 즉시 전멸, 요격대는 계속 전진해야
    advance_turn(s, [])
    assert not any(o.action.mode == "호송" for o in s.operations)
    interceptor = [o for o in s.operations if o.faction == "위"]
    assert interceptor and not interceptor[0].has_fought   # 유령 교전으로 임무 종료되지 않음
    assert not any("요격 완료" in h for h in s.history)


def test_sortie_regroups_into_city_after_fight():
    """⭐한 번 싸운 출성 부대는 위협 접근 중이어도 성내 복귀=재정비(성 밖 대기는 수비 분할만 지속 — 사용자).

    선타가 필요하면 예비 출성 규칙(도착 전 재출성)으로 다시 내보내는 게 정답."""
    s = _state()
    sortie = ActiveOperation(id=6, faction="위", stage="교전", threshold=0, committed_troops=3000,
                             committed_generals=[], has_fought=True,
                             action=Battle(kind="전투", mode="야전", origin="낙양", target="낙양", troops=3000))
    s.operations.append(sortie)
    s.operations.append(ActiveOperation(                # 촉 공성군 접근 중(이동) = 위협 잔존
        id=7, faction="촉", stage="이동", threshold=5, committed_troops=9000, committed_generals=[],
        action=Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=9000)))
    troops0 = s.cities["낙양"].troops
    advance_turn(s, [])
    assert not any(o.id == 6 for o in s.operations)     # 성내 복귀
    assert s.cities["낙양"].troops == troops0 + 3000    # 수비대 합류(재정비)


def test_two_factions_can_offer_surrender_to_same_target():
    s = _state()
    s.cities["건업"].troops = 100                       # 오=말기(도시1·국력 열세)
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="위")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="촉")
    assert len(s.proposals) == 2                        # 발신자 다르면 중복 아님
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="위")
    assert len(s.proposals) == 2 and any("중복" in h for h in s.history)   # 같은 발신자만 기각


# ---------- 카테고리 phase(외교→전투→내정) + 시드 재현성 ----------
def test_diplomacy_phase_precedes_battle():
    """⭐파기를 공성 '뒤에' 적어도 외교 phase가 먼저 → 파기→공격이 순서 무관 성립."""
    s = _state()
    s.alliances.append(("위", "촉"))
    advance_turn(s, {"촉": [
        Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=10000, generals=[]),
        Diplomacy(kind="외교", target_faction="위", proposal="파기"),
    ]})
    assert len(s.operations) == 1                       # 종전 순서면 동맹 공성 기각이었을 상황
    assert not s.alliances


def test_turn_order_seeded_reproducible():
    """세력 간 처리 순서=시드 셔플 — 같은 시드·같은 명령이면 결과(히스토리)가 완전 재현."""
    def run(seed: int) -> list[str]:
        s = _state(seed=seed)
        advance_turn(s, {
            "위": [Battle(kind="전투", mode="공성", origin="낙양", target="성도", troops=8000, generals=[])],
            "촉": [Battle(kind="전투", mode="공성", origin="성도", target="낙양", troops=8000, generals=[])],
        })
        advance_turn(s, [])
        return s.history
    assert run(7) == run(7)


# ---------- 항복 게이트: 도시 기준 ----------
def test_surrender_gate_city_count_and_power():
    s = _state()
    assert surrender_gate(s, "위", "오") is True        # 오 도시 1 + 위 국력 우위
    assert surrender_gate(s, "촉", "오") is False       # 국력 비등(우위 아님) → 불성립
    s.cities["시상"] = City(name="시상", owner="오", troops=100)
    s.cities["강하"] = City(name="강하", owner="오", troops=100)
    s.cities["여강"] = City(name="여강", owner="오", troops=100)
    assert surrender_gate(s, "위", "오") is False       # 도시 4 > 2 → 국력 무관 불성립
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="위")
    assert not s.proposals and any("기울지 않음" in h for h in s.history)
