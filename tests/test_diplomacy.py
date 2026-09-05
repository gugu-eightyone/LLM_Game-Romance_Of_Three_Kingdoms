"""외교(동맹·파기·포로반환) 테스트 — 제안/응답·전투 게이트·배신 콤보·몸값·멸망 해소. 전부 오프라인."""
from src.engine import (
    advance_turn, allied, apply_diplomacy, apply_disposition, respond_proposal, _city_threats,
)
from src.models import Battle, City, Diplomacy, Faction, GameState, General, Proposal


def _state(seed: int = 0) -> GameState:
    """위·촉·오 삼각 인접(낙양-건업만 거리 2 = 도로 교차 요격 시험용)."""
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


def _ally(s: GameState, a: str, b: str) -> None:
    s.alliances.append(tuple(sorted((a, b))))


# ---------- 제안/응답 ----------
def test_alliance_proposal_accept_and_chronicle():
    s = _state()
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="동맹",
                                 message="함께 조조를 치자"), actor="촉")
    assert len(s.proposals) == 1 and s.proposals[0].to_faction == "오"
    assert respond_proposal(s, s.proposals[0], True) is True
    assert allied(s, "촉", "오") and allied(s, "오", "촉")
    assert any("동맹 체결" in c for c in s.chronicle)


def test_alliance_reject_leaves_history_only():
    s = _state()
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="동맹"), actor="촉")
    respond_proposal(s, s.proposals[0], False, "위협이 더 급하지 않다")
    assert not allied(s, "촉", "오") and not s.proposals
    assert any("거절" in h for h in s.history)


def test_break_alliance_and_hallucination_guards():
    s = _state()
    _ally(s, "촉", "오")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="파기"), actor="촉")
    assert not allied(s, "촉", "오") and any("동맹 파기" in c for c in s.chronicle)
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="위", proposal="파기"), actor="촉")
    assert any("[환각]" in h and "파기" in h for h in s.history)     # 무동맹 파기
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="촉", proposal="동맹"), actor="촉")
    assert any("대상 무효" in h for h in s.history)                  # 자기 자신


def test_invalid_envoy_dropped_but_proposal_stands():
    s = _state()
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="동맹",
                                 envoy="여몽"), actor="촉")          # 타국 장수 사신
    assert len(s.proposals) == 1 and s.proposals[0].envoy == ""
    assert any("사신" in h and "[환각]" in h for h in s.history)


# ---------- 전투 게이트 ----------
def test_ally_siege_rejected_then_betrayal_combo_works():
    s = _state()
    _ally(s, "촉", "오")
    atk = Battle(kind="전투", mode="공성", origin="성도", target="건업", troops=10000, generals=[])
    advance_turn(s, {"촉": [atk]})
    assert not s.operations and any("동맹" in h and "[기각]" in h for h in s.history)
    # 같은 턴 "파기 → 공격" = 기습 배신 콤보(명령 순서대로 즉시 처리)
    s2 = _state()
    _ally(s2, "촉", "오")
    advance_turn(s2, {"촉": [Diplomacy(kind="외교", target_faction="오", proposal="파기"), atk]})
    assert len(s2.operations) == 1 and not allied(s2, "촉", "오")
    assert any("동맹 파기" in c for c in s2.chronicle)


def test_allied_road_crossing_does_not_engage():
    ops = [Battle(kind="전투", mode="야전", origin="낙양", target="건업", troops=5000, generals=[]),
           Battle(kind="전투", mode="야전", origin="건업", target="낙양", troops=5000, generals=[])]
    s = _state()                                       # 대조군: 동맹 없음 → 거리2 도로에서 교차 요격
    advance_turn(s, ops)
    advance_turn(s, [])
    assert any("[야전]" in h for h in s.history)
    s2 = _state()                                      # 동맹 → 같은 상황에서 안 싸움
    _ally(s2, "위", "오")
    advance_turn(s2, ops)
    advance_turn(s2, [])
    assert not any("[야전]" in h for h in s2.history)


def test_ally_rescue_engages_besieger_not_city():
    """촉이 오의 건업 공성 → 동맹 위의 야전 구원군이 공성군과 교전(도시 합류는 안 함)."""
    s2 = _state()
    _ally(s2, "위", "오")
    advance_turn(s2, [
        Battle(kind="전투", mode="공성", origin="성도", target="건업", troops=14000, generals=[]),
    ])
    troops0 = s2.cities["건업"].troops
    advance_turn(s2, [Battle(kind="전투", mode="야전", origin="낙양", target="건업",
                             troops=8000, generals=["장료"])])
    advance_turn(s2, [])
    assert any("구원군" in h and "야전 교전" in h for h in s2.history)
    assert s2.cities["건업"].troops <= troops0         # 위군이 건업 수비대로 합류(가산)하지 않음
    assert any("[야전]" in h for h in s2.history)      # 공성군(촉)과 실제 교전


def test_ally_approach_is_not_a_threat():
    from src.decide import brief
    s = _state()
    _ally(s, "위", "오")
    advance_turn(s, [Battle(kind="전투", mode="야전", origin="낙양", target="건업",
                            troops=5000, generals=[])])
    assert _city_threats(s, "건업", "오") == []
    text = brief(s, "오")
    assert "⚠피침" not in text and "동맹=위" in text


# ---------- 포로반환 ----------
def test_ransom_flow_moves_gold_and_returns_prisoner():
    s = _state()
    s.cities["건업"].prisoners.append("조운")
    s.cities["성도"].generals.remove("조운")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=1000), actor="촉")
    assert len(s.proposals) == 1
    assert respond_proposal(s, s.proposals[0], True) is True
    assert s.cities["성도"].gold == 2000 and s.cities["건업"].gold == 5000
    assert "조운" in s.cities["성도"].generals and "조운" not in s.cities["건업"].prisoners
    assert any("몸값" in c and "반환" in c for c in s.chronicle)


def test_ransom_rejected_when_cannot_pay_or_not_held():
    s = _state()
    s.cities["건업"].prisoners.append("조운")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=99999), actor="촉")
    assert not s.proposals and any("지불 여력 부족" in h for h in s.history)
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="여몽", offer_gold=100), actor="촉")   # 남의 장수
    assert not s.proposals and any("[환각]" in h and "반환" in h for h in s.history)


def test_ransom_fizzles_if_prisoner_gone_at_response():
    s = _state()
    s.cities["건업"].prisoners.append("조운")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=1000), actor="촉")
    apply_disposition(s, "건업", "조운", "처형")       # 응답 전에 처형됨
    assert respond_proposal(s, s.proposals[0], True) is False
    assert s.cities["성도"].gold == 3000               # 몸값 미이동
    assert any("무산" in h for h in s.history)


def test_ransom_pay_city_and_brief_captive_line():
    """⭐2026-09-05: 몸값 지불 도시 지정=그 도시에서만 차감 + brief [아군 피랍] 결론 줄(없으면 줄 자체 없음)."""
    from src.decide import brief
    s = _state()
    s.cities["강주"] = City(name="강주", owner="촉", troops=1000, gold=1500, food=2000)
    s.cities["건업"].prisoners.append("조운")
    s.cities["성도"].generals.remove("조운")
    text = brief(s, "촉")
    assert "[아군 피랍]" in text and "조운(오 건업 수감)" in text and "포로반환" in text
    assert "[아군 피랍]" not in brief(s, "위")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=1000, pay_city="강주"), actor="촉")
    assert respond_proposal(s, s.proposals[0], True) is True
    assert s.cities["강주"].gold == 500 and s.cities["성도"].gold == 3000   # 지정 도시만 차감(자동이면 성도)


def test_ransom_pay_city_insufficient_or_invalid():
    s = _state()
    s.cities["강주"] = City(name="강주", owner="촉", troops=1000, gold=500, food=100)
    s.cities["건업"].prisoners.append("조운")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=1000, pay_city="강주"), actor="촉")
    assert not s.proposals and any("강주 기준" in h for h in s.history)     # 지정 도시 기준으로 여력 검증
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="포로반환",
                                 prisoner="조운", offer_gold=1000, pay_city="건업"), actor="촉")
    assert len(s.proposals) == 1 and s.proposals[0].pay_city == ""          # 타국 도시 지정=자동 강등
    assert any("[환각]" in h and "지불 도시" in h for h in s.history)


def test_imprison_disposition_keeps_prisoner():
    s = _state()
    s.cities["건업"].prisoners.append("조운")
    assert apply_disposition(s, "건업", "조운", "수감") is True
    assert "조운" in s.cities["건업"].prisoners


# ---------- 항복 권유 ----------
def test_surrender_gate_rejects_when_even():
    """⭐비등하면 확률 0이 아니라 제안 자체가 결정론 기각(게이트)."""
    s = _state()
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="촉")
    assert not s.proposals and any("기울지 않음" in h for h in s.history)


def test_surrender_accept_transfers_everything():
    s = _state()
    s.cities["건업"].troops = 1000                     # 위 국력 25000 ≥ 3 × 오 6000
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    s.cities["건업"].generals.append("손권")
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유",
                                 message="천명은 위에 있다"), actor="위")
    assert len(s.proposals) == 1
    assert respond_proposal(s, s.proposals[0], True) is True
    assert s.cities["건업"].owner == "위" and not s.factions["오"].alive
    assert s.generals["여몽"].faction == "위"
    assert s.generals["손권"].faction == "위" and not s.generals["손권"].is_ruler   # 군주=신하로
    assert any("항복" in c for c in s.chronicle)


def test_surrender_reject_changes_nothing():
    s = _state()
    s.cities["건업"].troops = 1000
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="항복권유"), actor="위")
    respond_proposal(s, s.proposals[0], False, "아직 강동의 혼은 죽지 않았다")
    assert s.cities["건업"].owner == "오" and s.factions["오"].alive
    assert any("거절" in h for h in s.history)


# ---------- 멸망·드라이버 ----------
def test_fallen_faction_alliances_and_proposals_dissolve():
    s = _state()
    _ally(s, "위", "오")
    s.proposals.append(Proposal(from_faction="촉", to_faction="오", proposal="동맹"))
    s.cities["건업"].troops = 100                      # 오 유일 도시를 초약체로
    advance_turn(s, [Battle(kind="전투", mode="공성", origin="성도", target="건업",
                            troops=15000, generals=["조운"])])
    for _ in range(6):
        if not s.factions["오"].alive:
            break
        advance_turn(s, [])
    assert not s.factions["오"].alive
    assert s.alliances == [] and s.proposals == []


def test_resolve_proposals_skips_player_targeted(monkeypatch):
    """플레이어가 받는 제안(항복 권유 포함)은 LLM 응답 없이 큐 잔존 → 결과 창에서 직접 수락/거절."""
    import src.decide as decide
    monkeypatch.setattr(decide, "structured_complete",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("플레이어 몫에 LLM 호출 금지")))
    s = _state()
    s.proposals.append(Proposal(from_faction="위", to_faction="오", proposal="항복권유"))
    decide.resolve_proposals(s, player="오")
    assert len(s.proposals) == 1 and s.factions["오"].alive


def test_resolve_proposals_applies_and_holds(monkeypatch):
    import src.decide as decide
    s = _state()
    s.proposals.append(Proposal(from_faction="촉", to_faction="오", proposal="동맹"))
    monkeypatch.setattr(decide, "structured_complete",
                        lambda fmt, sys, usr, **kw: decide.ProposalResponse(accept=True, reason="촉과 손잡자"))
    decide.resolve_proposals(s)
    assert allied(s, "촉", "오") and not s.proposals

    from src.llm import LLMError
    s2 = _state()
    s2.proposals.append(Proposal(from_faction="촉", to_faction="오", proposal="동맹"))
    monkeypatch.setattr(decide, "structured_complete",
                        lambda *a, **kw: (_ for _ in ()).throw(LLMError("죽음")))
    decide.resolve_proposals(s2)
    assert len(s2.proposals) == 1                      # 보류=잔존(다음 턴 재질의)
