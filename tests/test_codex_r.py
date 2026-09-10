"""Codex 확정 결함 R-001~010 회귀 테스트 (2026-09-10 수리분).

각 테스트는 CODEX_REVIEW.md의 재현 구성을 최소화한 것 — 수리 전엔 실패, 수리 후 통과.
"""
import pytest

from src.decide import JudgeScore, JudgeScores, turn_judge
from src.engine import (_return_home, _succeed_ruler, advance_turn,
                        apply_diplomacy, respond_proposal, start_operation)
from src.llm import LLMError
from src.models import (ActiveOperation, Battle, City, Diplomacy, Domestic,
                        Faction, GameState, General, Proposal, Transfer)


def _state(**cities):
    names = list(cities)
    return GameState(
        cities={n: c for n, c in cities.items()},
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비"),
                  "오": Faction(name="오", ruler="손권")},
        distances={n: {m: 2 for m in names if m != n} for n in names},
    )


def _op(oid, faction, mode, origin, target, troops, stage="이동", **kw):
    return ActiveOperation(
        id=oid, faction=faction, stage=stage, progress=0, threshold=2,
        committed_troops=troops,
        action=Battle(kind="전투", mode=mode, origin=origin, target=target, troops=troops), **kw)


# ---------- R-001: 멸망 흡수된 작전의 같은 턴 재실행 ----------

def test_absorbed_op_does_not_siege_after_extinction():
    """촉의 마지막 도시가 먼저 함락 → 흡수된 촉 공성 작전이 오 도시를 함락시키면 안 된다."""
    s = _state(성도=City(name="성도", owner="촉", troops=100, wall_hp=1),
               허창=City(name="허창", owner="위", troops=100),
               건업=City(name="건업", owner="오", troops=100, wall_hp=1))
    s.operations.append(_op(1, "위", "공성", "허창", "성도", 20000, stage="교전"))
    s.operations.append(_op(2, "촉", "공성", "성도", "건업", 20000, stage="교전"))
    advance_turn(s, [])
    assert s.factions["촉"].alive is False
    assert s.cities["건업"].owner == "오"                # 수리 전: 망한 촉이 건업 함락
    assert not any(o.faction == "촉" for o in s.operations)


# ---------- R-002: 음수 금액 ----------

def test_negative_domestic_spend_rejected():
    s = _state(허창=City(name="허창", owner="위", troops=1000, gold=1000))
    advance_turn(s, {"위": [Domestic(kind="내정", city="허창", item="모병", gold_spent=-5000)]})
    assert s.cities["허창"].gold == 1000                 # 수리 전: 금 6000 + math domain error
    assert any("[환각]" in h and "음수" in h for h in s.history)


def test_negative_ransom_rejected():
    s = _state(성도=City(name="성도", owner="촉", gold=1000),
               건업=City(name="건업", owner="오", gold=1000, prisoners=["관우"]))
    s.generals["관우"] = General(name="관우", faction="촉")
    apply_diplomacy(s, Diplomacy(kind="외교", proposal="포로반환", target_faction="오",
                                 prisoner="관우", offer_gold=-2000), actor="촉")
    assert not s.proposals                               # 수리 전: 수락 시 역방향 이전(오 금 -1000)
    assert any("[환각]" in h and "음수" in h for h in s.history)


# ---------- R-003: 중복 장수 목록 ----------

def test_duplicate_generals_deduped_no_crash():
    s = _state(허창=City(name="허창", owner="위", troops=10000, generals=["장료"]),
               완=City(name="완", owner="촉", troops=1000))
    s.generals["장료"] = General(name="장료", faction="위")
    op = start_operation(s, Battle(kind="전투", mode="공성", origin="허창", target="완",
                                   troops=100, generals=["장료", "장료"]))
    assert op is not None and op.committed_generals == ["장료"]   # 수리 전: ValueError + 반쪽 상태
    assert s.cities["허창"].generals == [] and s.cities["허창"].troops == 9900


# ---------- R-004: 항복 부대가 제3세력 도시로 합류 ----------

def test_surrender_units_join_own_faction_city_only():
    """위 원정군의 출발지가 오에 넘어간 뒤 위가 촉에 항복 — 부대는 위(→촉) 도시로 가야 한다."""
    s = _state(장안=City(name="장안", owner="오", troops=500),
               완=City(name="완", owner="위", troops=0),
               성도=City(name="성도", owner="촉", troops=1000),
               건업=City(name="건업", owner="오", troops=1000))
    s.generals["우금"] = General(name="우금", faction="위")
    op = _op(1, "위", "공성", "장안", "성도", 1000)
    op.committed_generals = ["우금"]
    s.operations.append(op)
    respond_proposal(s, Proposal(from_faction="촉", to_faction="위", proposal="항복권유"), True)
    assert s.cities["장안"].troops == 500                # 수리 전: 오 도시에 1000 합류
    assert s.cities["완"].troops == 1000 and "우금" in s.cities["완"].generals
    assert s.generals["우금"].faction == "촉"


# ---------- R-005: 전멸 부대의 무효 후속 교전 ----------

def test_annihilated_op_skips_later_pairs():
    s = _state(상채=City(name="상채", owner="중립"),
               허창=City(name="허창", owner="위", troops=100),
               성도=City(name="성도", owner="촉", troops=100),
               건업=City(name="건업", owner="오", troops=100))
    s.operations.append(_op(1, "위", "야전", "허창", "상채", 1, stage="교전"))
    s.operations.append(_op(2, "촉", "야전", "성도", "상채", 10000, stage="교전"))
    s.operations.append(_op(3, "오", "야전", "건업", "상채", 10000, stage="교전"))
    advance_turn(s, [])
    fights = [h for h in s.history if h.startswith("[야전]") and "작전1" in h]
    assert len(fights) == 1 and "작전2" in fights[0]     # 수리 전: 전멸 후 작전1↔작전3도 처리(격멸자 덮어쓰기)


# ---------- R-007: 이감 중 포로가 군주 승계 ----------

def test_cargo_prisoner_excluded_from_succession():
    s = _state(허창=City(name="허창", owner="위", troops=1000, generals=["조인"]),
               성도=City(name="성도", owner="촉", troops=1000),
               한중=City(name="한중", owner="촉", troops=1000))
    s.generals["하후연"] = General(name="하후연", faction="위", command=100)   # 촉이 이감 중
    s.generals["조인"] = General(name="조인", faction="위", command=50)
    s.operations.append(ActiveOperation(
        id=1, faction="촉", stage="이동", progress=0, threshold=2, committed_troops=200,
        cargo_prisoners=["하후연"],
        action=Transfer(kind="호송", origin="성도", target="한중", troops=200)))
    _succeed_ruler(s, "위")
    assert s.factions["위"].ruler == "조인"              # 수리 전: 수감 중인 하후연이 군주


# ---------- R-008: 고립 호송 회군 시 화물 포로 소실 ----------

def test_stranded_convoy_frees_prisoner_nationwide():
    """양단이 모두 적 소유 + 인접에 원 세력 도시 없음 — 먼 원 세력 도시로라도 해방(무증발)."""
    s = GameState(
        cities={"장안": City(name="장안", owner="오"), "낙양": City(name="낙양", owner="오"),
                "성도": City(name="성도", owner="촉", troops=1000)},
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비"),
                  "오": Faction(name="오", ruler="손권")},
        distances={"장안": {"낙양": 3}, "낙양": {"장안": 3}, "성도": {}},
    )
    s.generals["관우"] = General(name="관우", faction="촉")
    op = ActiveOperation(
        id=1, faction="위", stage="이동", progress=0, threshold=3, committed_troops=200,
        cargo_prisoners=["관우"],
        action=Transfer(kind="호송", origin="장안", target="낙양", troops=200))
    s.operations.append(op)
    _return_home(s, op, "회군")
    assert "관우" in s.cities["성도"].generals           # 수리 전: 전 목록에서 소실(무로그 증발)


# ---------- R-009: 항복 후 승자 감옥의 신규 아군 잔류 ----------

def test_surrender_frees_prisoners_in_winner_jails():
    s = _state(허창=City(name="허창", owner="위", troops=10000, prisoners=["강유"]),
               성도=City(name="성도", owner="촉", troops=100),
               건업=City(name="건업", owner="오", troops=100))
    s.generals["강유"] = General(name="강유", faction="촉")
    respond_proposal(s, Proposal(from_faction="위", to_faction="촉", proposal="항복권유"), True)
    assert s.generals["강유"].faction == "위"
    assert "강유" not in s.cities["허창"].prisoners      # 수리 전: 아군인데 자기 감옥에 잔류
    assert "강유" in s.cities["허창"].generals


# ---------- R-006 + R-010: 심판 캐시 키 충돌 · 폴백 정보 집합 ----------

def test_same_strategy_text_scores_bind_per_action(monkeypatch):
    """같은 내용의 전략문 2건 — 점수가 각 명령에 결속(덮어쓰기·순서 밀림 없음).

    ⭐R-006 3차(Codex 재재검증 반례): 실제 구조화 출력 경로(model_validate_json)는 같은 내용의
    전략문에 **동일 str 객체를 재사용**한다 — 그 조건 그대로 파싱해서 검증한다.
    """
    import json

    from src.decide import Decision
    s = _state(허창=City(name="허창", owner="위", troops=10000),
               완=City(name="완", owner="촉", troops=100),
               섭=City(name="섭", owner="촉", troops=100))
    monkeypatch.setattr("src.decide.structured_complete",
                        lambda *a, **k: JudgeScores(scores=[JudgeScore(score=2), JudgeScore(score=10)]))
    d = Decision.model_validate_json(json.dumps({"actions": [
        {"kind": "전투", "mode": "공성", "origin": "허창", "target": "완",
         "troops": 100, "generals": [], "strategy": "같은 글"},
        {"kind": "전투", "mode": "공성", "origin": "허창", "target": "섭",
         "troops": 9000, "generals": [], "strategy": "같은 글"}]}))
    a1, a2 = d.actions
    judge = turn_judge(s, {"위": [a1, a2]})              # 내부에서 중복 객체를 분리(불변식 복원)
    assert judge(s, "위", "전투", a1.strategy)[0] == 2   # 수리 전: pydantic 객체 공유로 둘 다 10
    assert judge(s, "위", "전투", a2.strategy)[0] == 10


def test_rejected_action_does_not_shift_scores(monkeypatch):
    """R-006 재검증 반례(Codex): 같은 전략문의 앞 명령이 엔진 가드에서 기각돼도
    뒤의 유효 명령은 자기 점수(10)를 받는다 — FIFO였다면 앞 점수(2)로 밀렸다."""
    s = _state(허창=City(name="허창", owner="위", troops=10000),
               완=City(name="완", owner="촉", troops=100))
    monkeypatch.setattr("src.decide.structured_complete",
                        lambda *a, **k: JudgeScores(scores=[JudgeScore(score=2), JudgeScore(score=10)]))
    same = lambda: "".join(["같은", " 글"])
    actions = {"위": [
        Battle(kind="전투", mode="공성", origin="없는성", target="완", troops=100, strategy=same()),
        Battle(kind="전투", mode="공성", origin="허창", target="완", troops=9000, strategy=same())]}
    judge = turn_judge(s, actions)
    advance_turn(s, actions, judge=judge)
    op = next(o for o in s.operations if o.faction == "위")
    assert op.strategy_mod > 0.29                        # 10점 → +30% (수리 전: 2점 → −30%)


def test_fallback_judge_uses_turn_start_brief(monkeypatch):
    """일괄 실패 → 단건 폴백이 실행 도중의 상태가 아니라 턴 시작 브리핑을 근거로 채점."""
    s = _state(허창=City(name="허창", owner="위", troops=10000),
               완=City(name="완", owner="촉", troops=100))

    def batch_fail(*a, **k):
        raise LLMError("batch down")
    monkeypatch.setattr("src.decide.structured_complete", batch_fail)
    actions = {"위": [Battle(kind="전투", mode="공성", origin="허창", target="완",
                             troops=2000, strategy="돌격")]}
    judge = turn_judge(s, actions)                       # 배치 실패 → 캐시 빈 채 스냅샷만 보유

    captured = {}

    def single_ok(model_cls, system, user, **k):
        captured["user"] = user
        return JudgeScore(score=5, reason="")
    monkeypatch.setattr("src.decide.structured_complete", single_ok)
    s.cities["허창"].troops = 1234                       # 명령 실행으로 변한 상태 시뮬레이션
    assert judge(s, "위", "전투", "돌격") == (5, "")
    assert "병10000" in captured["user"]                 # 턴 시작 정보 집합
    assert "병1234" not in captured["user"]              # 수리 전: 변경 중 상태를 재직렬화
