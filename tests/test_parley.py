"""담화(포로 즉결 처분) 테스트 — 확률식·처분 효과·명령 차감·전 군주 낙인·담화 루프. 전부 오프라인."""
import pytest

from src.config import MAX_ORDERS_PER_TURN, PERSUADE_BASE
from src.engine import (
    apply_disposition, pending_dispositions, persuade_chance, _take_prisoner, advance_turn,
)
from src.models import City, Domestic, Faction, GameState, General


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


def test_persuade_success_recruits_and_debits():
    s = _state(seed=1)                                 # Random(1) 첫 난수 ≈0.134 < p≈0.517
    assert apply_disposition(s, "수춘", "감녕", "설득", persuader="순욱") is True
    assert "감녕" in s.cities["수춘"].generals and s.generals["감녕"].faction == "위"
    assert any("귀순" in c for c in s.chronicle)
    assert s.order_debits["위"] == 1                   # 성패 무관 차감


def test_persuade_failure_keeps_prisoner_and_debits():
    s = _state(seed=0)                                 # Random(0) 첫 난수 ≈0.844 > p
    assert apply_disposition(s, "수춘", "감녕", "설득", persuader="순욱") is False
    assert "감녕" in s.cities["수춘"].prisoners        # 잔존 → 다음 턴 재질의
    assert s.order_debits["위"] == 1
    assert pending_dispositions(s) == [("수춘", "감녕")]


def test_persuade_impossible_pick_is_hallucination_no_debit():
    s = _state()
    assert apply_disposition(s, "수춘", "감녕", "설득", persuader="여몽") is False  # 무효 주체
    assert "위" not in s.order_debits                  # 기각은 무료(환각 로그만)
    assert any("[환각]" in h for h in s.history)


def test_persuade_ruler_pick_rejected():
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "수춘", "손권")
    assert apply_disposition(s, "수춘", "손권", "설득", persuader="순욱") is False  # 군주=설득 불가
    assert "손권" in s.cities["수춘"].prisoners


# ---------- 명령 상한 차감 ----------
def test_order_debit_shrinks_next_turn_cap():
    s = _state(seed=0)
    apply_disposition(s, "수춘", "감녕", "설득", persuader="순욱")   # 실패 → 차감 1
    orders = [Domestic(kind="내정", city="수춘", item="모병", gold_spent=100)
              for _ in range(MAX_ORDERS_PER_TURN)]
    before = s.cities["수춘"].troops
    advance_turn(s, {"위": orders})
    assert s.cities["수춘"].troops == before + 200 * (MAX_ORDERS_PER_TURN - 1)  # 1건 잘림
    assert any("[담화]" in h and "차감" in h for h in s.history)
    assert not any("[환각] 위 명령" in h for h in s.history)   # 차감 컷은 환각 아님
    assert "위" not in s.order_debits                  # 차감분 소비 후 소멸


# ---------- 질의 드라이버(decide) ----------
def test_resolve_dispositions_applies_llm_choice(monkeypatch):
    import src.decide as decide
    monkeypatch.setattr(decide, "structured_complete",
                        lambda fmt, sys, usr, **kw: decide.Disposition(choice="처형", reason="본보기"))
    s = _state()
    decide.resolve_dispositions(s)
    assert "감녕" not in s.generals


def test_resolve_dispositions_holds_on_llm_failure(monkeypatch):
    import src.decide as decide
    from src.llm import LLMError

    def boom(*a, **kw):
        raise LLMError("죽음")
    monkeypatch.setattr(decide, "structured_complete", boom)
    s = _state()
    decide.resolve_dispositions(s)
    assert "감녕" in s.cities["수춘"].prisoners        # 보류=상태 무변


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
    assert s.order_debits["위"] == 1                   # 플레이어 담화도 시도=차감


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


def test_run_parley_refuses_ruler(monkeypatch):
    import src.parley as parley
    monkeypatch.setattr(parley, "structured_complete",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")))
    s = _state()
    s.generals["손권"] = General(name="손권", is_ruler=True, faction="오")
    _take_prisoner(s, "수춘", "손권")
    assert parley.run_parley(s, "수춘", "손권", player_lines=["항복하시오"], verbose=False) is False
