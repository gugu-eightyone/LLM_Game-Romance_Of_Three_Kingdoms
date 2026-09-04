"""배치 5(2026-09-05) — 계획 단위 일괄 채점(turn_judge) + 교전 상성(_matchup_mods). 전부 오프라인(가짜 LLM)."""
import src.decide as decide
from src.config import MATCHUP_MODIFIER_BOUND
from src.engine import advance_turn
from src.models import Battle, City, Domestic, Faction, GameState, General


def _state(seed: int = 0) -> GameState:
    return GameState(
        seed=seed,
        cities={
            "낙양": City(name="낙양", owner="위", troops=20000, gold=5000, food=8000, generals=["장료"]),
            "성도": City(name="성도", owner="촉", troops=15000, gold=9000, food=6000, generals=["조운"]),
        },
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비")},
        generals={"장료": General(name="장료", command=95, intel=70, faction="위"),
                  "조운": General(name="조운", command=91, intel=76, faction="촉")},
        distances={"낙양": {"성도": 2}, "성도": {"낙양": 2}},  # 거리 2 → 1턴 진행 후 중간 조우
    )


def _meet(s: GameState, strat_a: str, strat_b: str, matchup):
    """같은 길 반대 방향 진군 → 1턴 후 중간 조우(양쪽 progress 1+1 ≥ 거리 2)."""
    advance_turn(s, {
        "촉": [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                      troops=10000, generals=[], strategy=strat_a)],
        "위": [Battle(kind="전투", mode="야전", origin="낙양", target="성도",
                      troops=10000, generals=[], strategy=strat_b)],
    }, matchup=matchup)


# ---------- 교전 상성(_matchup_mods) ----------
def test_matchup_judged_once_cached_and_logged():
    s = _state()
    calls = []
    matchup = lambda st, ctx, fa, ta, fb, tb: calls.append(ctx) or (1, "갑이 을의 전제를 찌름")
    _meet(s, "북벽 강습", "성도 방면 요격", matchup)
    assert len(calls) == 1                             # 쌍당 1회
    assert len(s.matchup_cache) == 1
    assert abs(next(iter(s.matchup_cache.values()))) == MATCHUP_MODIFIER_BOUND
    assert any("[상성]" in h for h in s.history)       # 가시화
    advance_turn(s, [], matchup=matchup)               # 다음 턴 — 캐시로 재호출 없음
    assert len(calls) == 1


def test_matchup_skipped_when_a_side_has_no_strategy():
    s = _state()
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("무전략인데 상성 호출"))
    _meet(s, "북벽 강습", "", boom)                     # 한쪽 공란 → 호출 0
    assert s.matchup_cache == {}
    assert not any("[상성]" in h for h in s.history)


def test_matchup_ambiguous_is_zero_and_silent():
    s = _state()
    _meet(s, "북벽 강습", "요격", lambda *a: (0, ""))    # "없음" 판정
    assert next(iter(s.matchup_cache.values())) == 0.0  # 0으로 캐시(재호출 안 함)
    assert not any("[상성]" in h for h in s.history)    # 0은 로그 침묵(소음 방지)


# ---------- 계획 단위 일괄 채점(turn_judge) ----------
def _fake_llm(batch_scores):
    calls = {"batch": 0, "single": 0}

    def fake(fmt, system, user, **kw):
        if fmt is decide.JudgeScores:
            calls["batch"] += 1
            return decide.JudgeScores(
                scores=[decide.JudgeScore(score=x, reason="r") for x in batch_scores])
        calls["single"] += 1
        return decide.JudgeScore(score=5, reason="단건")
    return fake, calls


def _two_battles():
    mk = lambda tgt, strat: Battle(kind="전투", mode="공성", origin="성도", target=tgt,
                                   troops=4000, generals=[], strategy=strat)
    return {"촉": [mk("낙양", "동벽 견제"), mk("낙양", "서벽 주공")]}


def test_turn_judge_batches_one_call_and_falls_back_on_miss(monkeypatch):
    s = _state()
    fake, calls = _fake_llm([8, 3])
    monkeypatch.setattr(decide, "structured_complete", fake)
    judge = decide.turn_judge(s, _two_battles())
    assert calls["batch"] == 1                          # 2건 → 1호출
    assert judge(s, "촉", "전투", "동벽 견제") == (8, "r")
    assert judge(s, "촉", "전투", "서벽 주공") == (3, "r")
    assert calls["single"] == 0                         # 캐시 적중=추가 호출 없음
    assert judge(s, "촉", "전투", "배치에 없던 전략")[0] == 5  # 미스 → 단건 폴백
    assert calls["single"] == 1


def test_turn_judge_length_mismatch_falls_back(monkeypatch):
    s = _state()
    fake, calls = _fake_llm([8])                        # 2건 요청에 1점만 반환
    monkeypatch.setattr(decide, "structured_complete", fake)
    judge = decide.turn_judge(s, _two_battles())
    assert judge(s, "촉", "전투", "동벽 견제")[0] == 5   # 불일치 → 캐시 폐기, 단건 폴백
    assert calls["single"] == 1


def test_turn_judge_single_item_uses_legacy_path(monkeypatch):
    s = _state()
    fake, calls = _fake_llm([9])
    monkeypatch.setattr(decide, "structured_complete", fake)
    judge = decide.turn_judge(s, {"촉": [Domestic(kind="내정", city="성도", item="모병",
                                                  gold_spent=1000, strategy="정예 위주 소수 모병")]})
    assert calls["batch"] == 0                          # 단건=일괄 호출 안 함(기존 거동)
    assert judge(s, "촉", "내정", "정예 위주 소수 모병")[0] == 5
    assert calls["single"] == 1
