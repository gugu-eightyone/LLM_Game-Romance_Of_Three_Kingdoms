"""담화(포로 처분·설득 명령) 테스트 — 확률식·즉결 처분·설득=행동 턴 명령·담화 루프. 전부 오프라인."""
import pytest

from src.config import PERSUADE_BASE
from src.engine import (
    apply_disposition, pending_dispositions, persuade_chance, _take_prisoner, advance_turn,
)
from src.models import City, Faction, GameState, General, Persuade


def _state(seed: int = 0) -> GameState:
    """위가 수춘에 오 포로 감녕을 수감한 국면. 순욱(지95)이 설득 주체."""
    return GameState(
        seed=seed,
        cities={
            "수춘": City(name="수춘", owner="위", troops=10000, gold=5000,
                         generals=["순욱"], prisoners=["감녕"]),
            "건업": City(name="건업", owner="오", troops=8000, generals=[]),
        },
        factions={"위": Faction(name="위", ruler="조조"), "오": Faction(name="오", ruler="손권")},
        generals={"순욱": General(name="순욱", intel=95, faction="위"),
                  "감녕": General(name="감녕", intel=60, faction="오")},
        distances={"수춘": {"건업": 1}, "건업": {"수춘": 1}},
    )


# ---------- 확률식 ----------
def test_persuade_chance_uses_designated_persuader_and_loyalty():
    """⭐확률 = 지정 설득 장수 지력 × 포로 충의 감쇄(cap 폐지, §9-21 정정)."""
    s = _state()
    s.cities["수춘"].generals.append("악진")
    s.generals["악진"] = General(name="악진", intel=55, faction="위")
    assert persuade_chance(s, "수춘", "감녕", "순욱") == pytest.approx(
        (PERSUADE_BASE + 95 / 300) * 0.5)              # 감녕 충의 기본 50
    assert persuade_chance(s, "수춘", "감녕", "악진") == pytest.approx(
        (PERSUADE_BASE + 55 / 300) * 0.5)
    s.generals["감녕"].loyalty = 98                    # 관우급 충의 → 급감(천장 아니라 감쇄)
    assert persuade_chance(s, "수춘", "감녕", "순욱") == pytest.approx(
        (PERSUADE_BASE + 95 / 300) * 0.02)


def test_fallen_faction_removes_loyalty_shield():
    """⭐원 세력 멸망(도시 0)이면 충의 감쇄 미적용 — 지킬 주군이 없다(사용자 확정)."""
    s = _state()
    s.generals["감녕"].loyalty = 98                    # 관우급 충의라도
    s.factions["오"].alive = False                     # 오 멸망
    assert persuade_chance(s, "수춘", "감녕", "순욱") == pytest.approx(PERSUADE_BASE + 95 / 300)


def test_fallen_ruler_becomes_persuadable():
    """⭐망국 군주는 설득 가능(사용자 확정) — 귀순 시 군주 신분 소멸."""
    s = _state(seed=1)                                 # 첫 난수 ≈0.134 < 풀확률 ≈0.517
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오", loyalty=95)
    _take_prisoner(s, "수춘", "손권")
    s.cities["건업"].owner = "위"
    s.factions["오"].alive = False                     # 오 멸망
    assert persuade_chance(s, "수춘", "손권", "순욱") == pytest.approx(PERSUADE_BASE + 95 / 300)
    advance_turn(s, {"위": [Persuade(kind="설득", city="수춘", prisoner="손권", persuader="순욱")]})
    assert s.generals["손권"].faction == "위" and not s.generals["손권"].is_ruler


def test_persuade_chance_zero_without_agent_or_max_loyalty():
    s = _state()
    assert persuade_chance(s, "수춘", "감녕", "여몽") == 0.0   # 타지/미주둔 장수 지정
    assert persuade_chance(s, "수춘", "감녕", "") == 0.0       # 지정 안 함
    s2 = _state()
    s2.generals["감녕"].loyalty = 100                  # 절대 충의
    assert persuade_chance(s2, "수춘", "감녕", "순욱") == 0.0


def test_ruler_capture_defers_succession():
    """⭐포획=군주 신분 유지·승계 보류(§9-21 정정). 설득도 불가(is_ruler)."""
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    assert _take_prisoner(s, "수춘", "손권") is True
    assert s.generals["손권"].is_ruler                 # 신분 유지
    assert s.factions["오"].ruler == "손권"            # 승계 안 일어남
    assert persuade_chance(s, "수춘", "손권", "순욱") == 0.0


def test_release_ruler_restores_him():
    """석방된 군주는 군주 그대로 복귀."""
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "수춘", "손권")
    apply_disposition(s, "수춘", "손권", "석방")
    assert "손권" in s.cities["건업"].generals and s.generals["손권"].is_ruler
    assert s.factions["오"].ruler == "손권"


def test_execute_ruler_triggers_succession():
    """처형 확정 시점에만 승계(⭐즉시 승계 폐기)."""
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    s.generals["여몽"] = General(name="여몽", command=90, faction="오")
    s.cities["건업"].generals.append("여몽")
    _take_prisoner(s, "수춘", "손권")
    apply_disposition(s, "수춘", "손권", "처형")
    assert "손권" not in s.generals
    assert s.factions["오"].ruler == "여몽" and s.generals["여몽"].is_ruler
    assert any("여몽 군주 승계" in c for c in s.chronicle)


# ---------- 처분 효과 ----------
def test_execute_removes_from_roster_and_chronicles():
    s = _state()
    assert apply_disposition(s, "수춘", "감녕", "처형") is True
    assert "감녕" not in s.generals and "감녕" not in s.cities["수춘"].prisoners
    assert any("감녕 처형" in c for c in s.chronicle)


def test_release_returns_to_home_faction():
    s = _state()
    assert apply_disposition(s, "수춘", "감녕", "석방") is True
    assert "감녕" in s.cities["건업"].generals
    assert any("감녕 석방" in c for c in s.chronicle)


def test_release_without_home_goes_to_wilderness():
    s = _state()
    s.cities["건업"].owner = "위"                      # 오 도시 전무
    apply_disposition(s, "수춘", "감녕", "석방")
    assert all("감녕" not in c.generals for c in s.cities.values())   # 재야(로스터만 잔존)
    assert any("재야" in c for c in s.chronicle)


def test_persuade_action_recruits():
    """⭐설득 = 행동 턴의 명령(즉결 처분에서 분리) — 명령 슬롯을 자연 소모."""
    s = _state(seed=1)                                 # Random(1) 첫 난수 ≈0.134 < p≈0.258
    advance_turn(s, {"위": [Persuade(kind="설득", city="수춘", prisoner="감녕", persuader="순욱")]})
    assert "감녕" in s.cities["수춘"].generals and s.generals["감녕"].faction == "위"
    assert any("귀순" in c for c in s.chronicle)


def test_persuade_action_failure_keeps_prisoner():
    s = _state(seed=0)                                 # Random(0) 첫 난수 ≈0.844 > p
    advance_turn(s, {"위": [Persuade(kind="설득", city="수춘", prisoner="감녕", persuader="순욱")]})
    assert "감녕" in s.cities["수춘"].prisoners        # 잔존 → 재시도 가능
    assert pending_dispositions(s) == [("수춘", "감녕")]


def test_persuade_action_guards():
    s = _state()
    advance_turn(s, {"위": [Persuade(kind="설득", city="수춘", prisoner="감녕", persuader="여몽")]})
    assert "감녕" in s.cities["수춘"].prisoners        # 무효 주체(타지 장수) → 기각
    assert any("[환각]" in h for h in s.history)
    s2 = _state()
    advance_turn(s2, {"오": [Persuade(kind="설득", city="수춘", prisoner="감녕", persuader="순욱")]})
    assert any("[위반]" in h and "월권" in h for h in s2.history)   # 남의 도시 포로 설득


def test_persuade_ruler_pick_rejected():
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "수춘", "손권")
    advance_turn(s, {"위": [Persuade(kind="설득", city="수춘", prisoner="손권", persuader="순욱")]})
    assert "손권" in s.cities["수춘"].prisoners        # 현직 군주 = 설득 불가


# ---------- 질의 드라이버(decide) ----------
def test_resolve_dispositions_queries_new_captives_once(monkeypatch):
    import src.decide as decide
    monkeypatch.setattr(decide, "structured_complete",
                        lambda fmt, sys, usr, **kw: decide.Disposition(choice="처형", reason="본보기"))
    s = _state()
    s.pending_captives.append(("수춘", "감녕"))        # 신규 포획만 질의 대상
    decide.resolve_dispositions(s)
    assert "감녕" not in s.generals and not s.pending_captives


def test_resolve_dispositions_imprison_means_no_requery(monkeypatch):
    """수감 선택 → 질의 큐에서 빠짐(재질의 스팸 없음). 이후는 설득 명령·몸값의 영역."""
    import src.decide as decide
    monkeypatch.setattr(decide, "structured_complete",
                        lambda fmt, sys, usr, **kw: decide.Disposition(choice="수감", reason="몸값을 기다린다"))
    s = _state()
    s.pending_captives.append(("수춘", "감녕"))
    decide.resolve_dispositions(s)
    assert "감녕" in s.cities["수춘"].prisoners and not s.pending_captives


def test_resolve_dispositions_skips_player_faction(monkeypatch):
    """플레이어 세력의 포획자는 LLM에 안 묻고 큐에 잔존 → 턴 종료 결과 창이 소비(Q4 동형)."""
    import src.decide as decide
    monkeypatch.setattr(decide, "structured_complete",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("플레이어 몫에 LLM 호출 금지")))
    s = _state()
    s.pending_captives.append(("수춘", "감녕"))
    decide.resolve_dispositions(s, player="위")
    assert s.pending_captives == [("수춘", "감녕")]    # UI가 소비할 때까지 유지


def test_resolve_dispositions_holds_on_llm_failure(monkeypatch):
    import src.decide as decide
    from src.llm import LLMError

    def boom(*a, **kw):
        raise LLMError("죽음")
    monkeypatch.setattr(decide, "structured_complete", boom)
    s = _state()
    s.pending_captives.append(("수춘", "감녕"))
    decide.resolve_dispositions(s)
    assert "감녕" in s.cities["수춘"].prisoners        # 상태 무변
    assert s.pending_captives == [("수춘", "감녕")]    # 다음 턴 재질의


# ---------- 플레이어 담화 루프(parley) ----------
def test_score_to_chance_no_cap():
    """⭐상한 없음: 인물 난이도는 심판 채점에 내재(만점=100%)."""
    from src.parley import score_to_chance
    assert score_to_chance(1) == 0.0
    assert score_to_chance(3) == 0.5
    assert score_to_chance(5) == 1.0


def test_run_parley_headless(monkeypatch):
    import src.parley as parley

    def fake(fmt, sys, usr, **kw):
        if fmt is parley.ParleyReply:
            return parley.ParleyReply(text="…그 말이 맞을지도 모르오.")
        return parley.ParleyScore(score=5, reason="처지에 와닿는 설득")
    monkeypatch.setattr(parley, "structured_complete", fake)
    s = _state(seed=1)                                 # 첫 난수 ≈0.134 < 0.6
    ok = parley.run_parley(s, "수춘", "감녕", player_lines=["오의 대세는 기울었소"], verbose=False)
    assert ok and "감녕" in s.cities["수춘"].generals


def test_parley_prompts_carry_loyalty_and_persona(monkeypatch):
    """⭐장수별 프롬프트: 충의·페르소나가 포로 연기와 심판 채점 프롬프트에 실린다(§9-21 정정)."""
    import src.parley as parley
    seen = {}

    def fake(fmt, sys, usr, **kw):
        seen[fmt] = sys
        if fmt is parley.ParleyReply:
            return parley.ParleyReply(text="……")
        return parley.ParleyScore(score=1, reason="이 인물에겐 안 먹힘")
    monkeypatch.setattr(parley, "structured_complete", fake)
    s = _state()
    s.generals["감녕"].loyalty = 98
    s.generals["감녕"].persona = "강동의 맹장"
    parley.run_parley(s, "수춘", "감녕", player_lines=["천하를 논하자"], verbose=False)
    assert "강철" in seen[parley.ParleyReply] and "강동의 맹장" in seen[parley.ParleyReply]
    assert "98/100" in seen[parley.ParleyScore] and "강동의 맹장" in seen[parley.ParleyScore]


def test_parley_prompts_switch_to_fallen_mode(monkeypatch):
    """원 세력 멸망 시 연기·심판 프롬프트가 '망국 장수' 모드로 전환(충의 방패 해제)."""
    import src.parley as parley
    seen = {}

    def fake(fmt, sys, usr, **kw):
        seen[fmt] = sys
        if fmt is parley.ParleyReply:
            return parley.ParleyReply(text="……")
        return parley.ParleyScore(score=3)
    monkeypatch.setattr(parley, "structured_complete", fake)
    s = _state()
    s.generals["감녕"].loyalty = 98
    s.factions["오"].alive = False
    parley.run_parley(s, "수춘", "감녕", player_lines=["천하를 논하자"], verbose=False)
    assert "멸망" in seen[parley.ParleyReply] and "강철" not in seen[parley.ParleyReply]
    assert "멸망" in seen[parley.ParleyScore] and "98/100" not in seen[parley.ParleyScore]


def test_run_parley_refuses_ruler(monkeypatch):
    import src.parley as parley
    monkeypatch.setattr(parley, "structured_complete",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")))
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "수춘", "손권")
    assert parley.run_parley(s, "수춘", "손권", player_lines=["항복하시오"], verbose=False) is False
