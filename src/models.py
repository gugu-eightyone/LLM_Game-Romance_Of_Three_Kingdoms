"""LLM_Game v2 — 핵심 스키마 (Pydantic).

설계 근거: docs/DISCUSSION.md §8(신뢰경계)·§9(게임 설계).
- 상태(State)는 per-city 저장: 병사·식량·금이 도시에 소속(함락 시 약탈). 장수도 도시 주둔.
- LLM 출력(Action)은 discriminated union — 종류별 필드가 다르므로 flat+optional 대신 태그된 합집합.
- 신뢰경계: LLM은 "의도"(Action)만 제안, "진행도"는 엔진 상태(ActiveOperation)가 소유.
- 게임 로직 불변식(병사≥0, 금 보존 등)은 여기서 raise하지 않는다 — B층 평가가 "위반을 세야"
  하므로, 자원은 순수 int로 두고 위반 탐지는 eval 쪽 검사 함수가 담당. (사기만 하드 클램프 0~100.)
"""
from __future__ import annotations

import random
from typing import Literal, Union

from pydantic import BaseModel, Field, PrivateAttr, TypeAdapter

from .config import STRATEGY_MAX_CHARS  # 캘리브레이션 상수는 config.py에 집약

FactionName = Literal["위", "촉", "오", "중립"]


# ======================= 상태(State) — per-city 저장 =======================
class City(BaseModel):
    name: str
    owner: FactionName
    troops: int = 0          # 순수 int: 음수 탐지는 B층이 담당(여기서 clamp/raise 안 함)
    food: int = 0
    gold: int = 0
    wall: int = 0            # 성벽 레벨: 공성 수비 보정(내정 성벽보수로 증가)
    generals: list[str] = Field(default_factory=list)  # 주둔 장수(로스터 이름 참조)
    prisoners: list[str] = Field(default_factory=list)  # 이 도시에 수감된 포로(포획자=city.owner). 담화 co-location 기반. [[DISCUSSION#9-10]]


class Faction(BaseModel):
    name: FactionName
    ruler: str                                   # 현 군주 장수 이름(포획 시 최고통솔 자동 승계로 갱신). [[DISCUSSION#9-16]]
    morale: int = Field(default=50, ge=0, le=100)  # 사기(士氣, 전투 의지 → 전투력 배수 예정). 하드 바운드. ※民心(백성 지지)은 별개 미래 스탯. [[DISCUSSION#9-10]]
    alive: bool = True                           # 세력 소멸(전 도시 상실) 시 False. 승리=천하통일 단일화


class General(BaseModel):
    """장수 로스터(정적). 3스탯 — 통솔=군 전투(_power), 무력=일기토(예약·휴면), 지력=계략·정보. [[DISCUSSION#9-15]]"""
    name: str
    command: int = 50        # 통솔 = 군 전투 보정 (_power가 읽는 유일 스탯)
    might: int = 50          # 무력 = 일기토(개인 무예). 일기토 시스템 붙을 때까지 휴면
    intel: int = 50          # 지력 = 계략·정보(안개). 증분2 배선
    is_ruler: bool = False   # 군주 플래그. 포획 시 세력 자동 승계 트리거(멸망은 도시0일 때만). [[DISCUSSION#9-16]]
    faction: str = ""        # 소속 세력. 시나리오 로드 시 시작 주둔 도시 소유주로 파생(등용 시 갱신). 포로 해방 복귀지 판정용.
    loyalty: int = Field(default=50, ge=0, le=100)  # 충의: LLM 경로 확률 감쇄 + 페르소나 연기·심판 채점의 근거(⭐cap 폐지). 군주 설득 불가는 is_ruler. [[DISCUSSION#9-21]]
    persona: str = ""        # 큐레이트 한 줄(도원결의 등 기계적 일관성이 필요한 소수만). 나머지는 모델 내장지식이 페르소나(§9-7).


class ActiveOperation(BaseModel):
    """엔진이 굴리는 다-달 작전 상태. Action(의도) + 진행도(코드 소유).

    2단계 생애주기: 이동(거리 기반) → 교전(병력·전략 기반) → 해소. [[DISCUSSION#9-9]]
    """
    id: int                                      # ③ seam: 야전 요격이 이 작전을 콕 집게(증분2 수용)
    faction: FactionName                         # 작전 주체(출발도시 소유 세력)
    action: "Action"
    stage: Literal["이동", "교전"] = "이동"       # 작전 국면. 도착 시 이동→교전 전환
    progress: float = 0                          # float: 세력별 진군속도(오 1.25×)가 소수 진척 → 잉여 이월
    threshold: float                             # 진행도 ≥ threshold → 국면 전환/해소
    prep: float = 0                              # 조기도착 잉여(개월). 오만 >0 → 첫 교전 준비보정(토루). [[DISCUSSION#9-9]]
    committed_troops: int = 0                     # 검증·클램프된 실제 투입
    committed_generals: list[str] = Field(default_factory=list)  # 검증된 동행 장수
    unit_morale: int = 50                         # 부대 사기: 출전 시 전역 사기 복사→독립. 전투력 배수·매교전 소량 감소. [[DISCUSSION#9-10]]
    has_fought: bool = False                      # 야전 교전 1회+ 경험. True인 야전 op는 상대 소멸 시 출격 종료→복귀(공성은 임무 재개). [[DISCUSSION#9-10]]
    # --- 호송 화물(호송 op만 사용, 전투 op는 전부 0/빈) ---
    cargo_gold: int = 0
    cargo_food: int = 0
    cargo_prisoners: list[str] = Field(default_factory=list)


class GameState(BaseModel):
    year: int = 0
    month: int = 1
    seed: int = 0                                # 확률 판정(포로화 등) 재현용 씨앗. 엔진 소유 RNG. D층 100판 배치 재현성. [[DISCUSSION#9-10]]
    cities: dict[str, City] = Field(default_factory=dict)
    factions: dict[str, Faction] = Field(default_factory=dict)
    operations: list[ActiveOperation] = Field(default_factory=list)
    # --- 정적 세계 데이터(게임 중 불변). 시나리오에서 로드, State에 함께 실어 다님 ---
    distances: dict[str, dict[str, int]] = Field(default_factory=dict)  # 인접·거리(개월): city→neighbor→months
    river_edges: list[tuple[str, str]] = Field(default_factory=list)    # 강 구간(무순서 쌍): 도하 지연·수전 보정. [[DISCUSSION#9-9]]
    generals: dict[str, General] = Field(default_factory=dict)          # 장수+군주 로스터(통솔·무력·지력·is_ruler). 위치=주둔 도시의 generals 리스트
    # --- 진행/결과 ---
    next_op_id: int = 1                          # 작전 id 발급 카운터
    alliances: list[tuple[str, str]] = Field(default_factory=list)  # 동맹 쌍(정렬 튜플로 정규화). 효과=안 싸움+구원, 파기 즉시. [[DISCUSSION#9-22]]
    pending_captives: list[tuple[str, str]] = Field(default_factory=list)  # 신규 포획 (도시, 장수) — 즉결 처분 질의는 포획 시 1회만(수감 후엔 설득 명령·몸값 영역)
    proposals: list[Proposal] = Field(default_factory=list)         # 대기 외교 제안 큐(턴 해소 후 상대 군주 질의로 소진)
    winner: FactionName | None = None            # 승리 판정 결과(None=진행 중)
    history: list[str] = Field(default_factory=list)
    chronicle: list[str] = Field(default_factory=list)  # 주요 연혁(함락·군주 포획/승계·처형·멸망). 영구 보존, brief 전량 노출 → "장비의 원수"를 LLM이 기억. 요약 LLM 불필요.

    _rng: random.Random | None = PrivateAttr(default=None)  # 엔진 소유 시드 RNG(직렬화 제외)

    def model_post_init(self, __context: object) -> None:
        if self._rng is None:
            self._rng = random.Random(self.seed)

    @property
    def rng(self) -> random.Random:
        """포로화 등 확률 판정용 시드 고정 RNG(엔진 전용, LLM 관여 0)."""
        if self._rng is None:
            self._rng = random.Random(self.seed)
        return self._rng


# ======================= LLM 의도(Action) — discriminated union =======================
# 종류별 필드가 다르다. 전투(공성/야전)/계략/내정. 수성은 작전 아님(엔진 자동 방어).
class Battle(BaseModel):
    kind: Literal["전투"]
    mode: Literal["공성", "야전"]
    origin: str                                  # 출발도시(검증 기준: 병력·장수)
    target: str                                  # 공성=대상도시 / 야전=적 세력·방면
    origin_troops_seen: int = -1                 # 검산 칸: 브리핑의 출발도시 보유 병력을 베껴 적기(투입량 아님).
                                                 # 베껴 적는 행위가 보유량에 주의를 강제(과투입 환각 대책) + 불일치=측정 표면.
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
    item: Literal["식량증산", "모병", "사기진작", "성벽보수"]
    gold_spent: int = 0                           # 투입병력·대상적 없음
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)  # 표현만 자연어(효과는 item+gold 결정론). 심판 채점 재료.


class OpCommand(BaseModel):
    """작전지시: 진행 중인 자기 작전에 지시. 회군=철수(교전 중=퇴각 손실), 전략변경=전략문 교체.

    전략변경이 "전술 업그레이드해가며 싸우는 재미"의 인터페이스 — judge 배선 시 매 교전 라운드
    이 전략을 채점해 전투 보정(±STRATEGY_MODIFIER_BOUND)으로 실효 발생. [[DISCUSSION#9-12]]
    """
    kind: Literal["작전지시"]
    op_id: int                                   # 대상 작전 번호(brief [진행 중 작전]에 노출)
    order: Literal["전략변경", "회군"]
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)  # 전략변경일 때 새 전략문


class Persuade(BaseModel):
    """설득: 우리 도시에 수감된 포로의 등용 시도 — 행동 턴의 명령(⭐즉결 처분에서 분리, 명령 슬롯 자연 소모).

    확률 = engine.persuade_chance(지정 persuader의 지력 × 포로 충의 감쇄). 실패해도 포로 잔존.
    """
    kind: Literal["설득"]
    city: str                                    # 포로가 수감된 우리 도시
    prisoner: str
    persuader: str = ""                          # 담화를 맡을 그 도시 주둔 우리 장수(지력이 확률)
    strategy: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


class Diplomacy(BaseModel):
    """외교: 동맹 제안/파기/포로반환. 수락 판정=상대 군주 LLM(즉결 질의), 효과·검증=엔진. [[DISCUSSION#9-22]]

    파기는 즉시 효력(같은 턴 "파기→공격" 배신 콤보 성립 — 기록이 연혁에 남아 신뢰는 LLM이 판단).
    """
    kind: Literal["외교"]
    target_faction: FactionName
    proposal: Literal["동맹", "파기", "포로반환", "항복권유"]
    prisoner: str = ""                           # 포로반환: 되사올 우리 장수
    offer_gold: int = 0                          # 포로반환 몸값(금)
    offer_food: int = 0                          # 포로반환 몸값(식량)
    envoy: str = ""                              # 사신 장수(지정만 — 서사·C층 재료, 기계 효과 없음)
    message: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


class Proposal(BaseModel):
    """대기 중 외교 제안(동맹/포로반환). 턴 해소 후 드라이버가 상대 군주 LLM에 질의해 해소. [[DISCUSSION#9-22]]"""
    from_faction: str
    to_faction: str
    proposal: str = "동맹"
    prisoner: str = ""
    offer_gold: int = 0
    offer_food: int = 0
    envoy: str = ""
    message: str = ""


class Transfer(BaseModel):
    """호송: 병사·장수·포로·금·식량을 인접 아군 도시로 이동. 도착·요격 해소는 엔진.

    호위 규칙(엔진 검증): 병사·물자·포로를 실으면 최소 ESCORT_MIN_TROOPS, 장수 단독은 면제.
    """
    kind: Literal["호송"]
    mode: Literal["호송"] = "호송"                # 엔진 op.action.mode 분기 공용(공성/야전과 한 축)
    origin: str
    target: str                                  # 인접 아군 도시만
    troops: int = 0
    generals: list[str] = Field(default_factory=list)
    prisoners: list[str] = Field(default_factory=list)
    gold: int = 0
    food: int = 0


# 개념상 discriminated union(종류별 필드 다름)이나, discriminator를 스키마에 박으면
# pydantic이 `oneOf`를 내고 → OpenAI 구조화출력이 이를 거부(anyOf만 허용, 스모크로 확인).
# 세 변형의 `kind` 리터럴이 서로 겹치지 않아 discriminator 없는 평범한 Union으로도
# 판별이 정확·유일함(잘못된 조합은 그대로 거부). → anyOf로 나가 OpenAI 통과.
Action = Union[Battle, Scheme, Domestic, Transfer, OpCommand, Diplomacy, Persuade]
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

    # 5) 사기는 하드 바운드 → 초과는 거부
    try:
        Faction(name="촉", ruler="유비", morale=150)
        raise AssertionError("사기 150이 통과되면 안 됨")
    except Exception as e:
        assert not isinstance(e, AssertionError), e

    print("models.py self-check 통과 [OK]")


if __name__ == "__main__":
    demo()
