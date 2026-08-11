"""LLM_Game v2 — 핵심 스키마 (Pydantic).

설계 근거: docs/DISCUSSION.md §8(신뢰경계)·§9(게임 설계).
- 상태(State)는 per-city 저장: 병사·식량·금이 도시에 소속(함락 시 약탈). 장수도 도시 주둔.
- LLM 출력(Action)은 discriminated union — 종류별 필드가 다르므로 flat+optional 대신 태그된 합집합.
- 신뢰경계: LLM은 "의도"(Action)만 제안, "진행도"는 엔진 상태(ActiveOperation)가 소유.
- 게임 로직 불변식(병사≥0, 금 보존 등)은 여기서 raise하지 않는다 — B층 평가가 "위반을 세야"
  하므로, 자원은 순수 int로 두고 위반 탐지는 eval 쪽 검사 함수가 담당. (민심만 하드 클램프 0~100.)
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# --- 캘리브레이션 손잡이 (플레이테스트로 튜닝) ---
STRATEGY_MAX_CHARS = 50          # 전략 자유서술 상한. 담화 아닌 지시라 짧게. [[DISCUSSION#9-8]]
STRATEGY_MODIFIER_BOUND = 0.15   # judge 전략점수 → 전투 보정 상한(±). 결정론 코어가 지배하도록.

FactionName = Literal["위", "촉", "오", "중립"]


# ======================= 상태(State) — per-city 저장 =======================
class City(BaseModel):
    name: str
    owner: FactionName
    troops: int = 0          # 순수 int: 음수 탐지는 B층이 담당(여기서 clamp/raise 안 함)
    food: int = 0
    gold: int = 0
    generals: list[str] = Field(default_factory=list)  # 주둔 장수


class Faction(BaseModel):
    name: FactionName
    ruler: str                                   # 군주 이름 = 참수(패배) 대상
    morale: int = Field(default=50, ge=0, le=100)  # 민심: 하드 바운드(항상 클램프되는 값)
    prisoners: list[str] = Field(default_factory=list)
    alive: bool = True                           # 군주 참수 시 False → 세력 소멸


class ActiveOperation(BaseModel):
    """엔진이 굴리는 다-달 작전 상태. Action(의도) + 진행도(코드 소유)."""
    action: "Action"
    progress: int = 0
    threshold: int                               # 진행도 ≥ threshold → 해소(함락 등)
    committed_troops: int = 0                     # 검증·클램프된 실제 투입


class GameState(BaseModel):
    year: int = 0
    month: int = 1
    cities: dict[str, City] = Field(default_factory=dict)
    factions: dict[str, Faction] = Field(default_factory=dict)
    operations: list[ActiveOperation] = Field(default_factory=list)
    history: list[str] = Field(default_factory=list)


# ======================= LLM 의도(Action) — discriminated union =======================
# 종류별 필드가 다르다. 전투(공성/야전)/계략/내정. 수성은 작전 아님(엔진 자동 방어).
class Battle(BaseModel):
    kind: Literal["전투"]
    mode: Literal["공성", "야전"]
    origin: str                                  # 출발도시(검증 기준: 병력·장수)
    target: str                                  # 공성=대상도시 / 야전=적 세력·방면
    troops: int
    generals: list[str] = Field(default_factory=list)
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


class Scheme(BaseModel):
    kind: Literal["계략"]
    target_faction: FactionName
    scheme_type: Literal["밀정", "이간", "유언비어"]
    generals: list[str] = Field(default_factory=list)   # 책사 보정(투입병력 없음)
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


class Domestic(BaseModel):
    kind: Literal["내정"]
    city: str                                    # 자국 도시
    item: Literal["식량증산", "모병", "민심회복", "성벽보수"]
    gold_spent: int = 0                           # 투입병력·대상적 없음


# 개념상 discriminated union(종류별 필드 다름)이나, discriminator를 스키마에 박으면
# pydantic이 `oneOf`를 내고 → OpenAI 구조화출력이 이를 거부(anyOf만 허용, 스모크로 확인).
# 세 변형의 `kind` 리터럴이 서로 겹치지 않아 discriminator 없는 평범한 Union으로도
# 판별이 정확·유일함(잘못된 조합은 그대로 거부). → anyOf로 나가 OpenAI 통과.
Action = Union[Battle, Scheme, Domestic]
ActionAdapter = TypeAdapter(Action)              # dict → 올바른 변형으로 파싱·검증


# ======================= 태세(Stance) — 공성 피격 시 방어자 선택 =======================
class Stance(BaseModel):
    posture: Literal["농성", "출격", "위임"]      # 위임=자동·보정0·입력 막음
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


def demo() -> None:
    """스키마 self-check. `python -m src.models` 로 실행."""
    # 1) discriminated union이 종류로 올바른 변형을 고른다
    b = ActionAdapter.validate_python(
        {"kind": "전투", "mode": "공성", "origin": "성도", "target": "한중",
         "troops": 30000, "generals": ["조운"], "strategy": "서쪽 절반, 남쪽 퇴로 확보"}
    )
    assert isinstance(b, Battle) and b.mode == "공성"

    s = ActionAdapter.validate_python(
        {"kind": "계략", "target_faction": "위", "scheme_type": "이간"}
    )
    assert isinstance(s, Scheme)

    # 2) 잘못된 조합은 거부된다 (내정에 전투 필드 넣기 → 판별자 불일치/필드 부재)
    try:
        ActionAdapter.validate_python({"kind": "내정", "mode": "공성"})
        raise AssertionError("내정에 mode가 통과되면 안 됨")
    except Exception as e:
        assert not isinstance(e, AssertionError), e

    # 3) 전략 50자 초과는 검증 실패 (장황함/진시황 방어의 1차선)
    try:
        Battle(kind="전투", mode="야전", origin="업", target="촉",
               troops=1, strategy="가" * (STRATEGY_MAX_CHARS + 1))
        raise AssertionError("50자 초과가 통과되면 안 됨")
    except Exception as e:
        assert not isinstance(e, AssertionError), e

    # 4) 자원 int는 음수도 "구성"은 된다 (B층이 위반을 세야 하므로 raise 안 함)
    assert City(name="합비", owner="위", troops=-5).troops == -5

    # 5) 민심은 하드 바운드 → 초과는 거부
    try:
        Faction(name="촉", ruler="유비", morale=150)
        raise AssertionError("민심 150이 통과되면 안 됨")
    except Exception as e:
        assert not isinstance(e, AssertionError), e

    print("models.py self-check 통과 [OK]")


if __name__ == "__main__":
    demo()
