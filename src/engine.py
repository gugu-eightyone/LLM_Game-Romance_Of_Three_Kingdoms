"""LLM_Game v2 — 결정론 규칙 엔진 (증분 1).

설계: docs/DISCUSSION.md §9(게임 설계)·§9-9(규칙 엔진).
하이브리드 원칙: 숫자·판정은 전부 여기(코드, 결정론). LLM은 의도(Action)만 제안.
이 엔진이 곧 B층 평가표면 — 불변식 위반(음수 병력·유령 함락·순간이동)을 여기서 클램프+로깅한다.

증분 1 범위: 이동 → 공성 → 내정 → 승리/참수 판정.
야전 수용 3-seam(§9-9): ① _combat_round가 "군대 vs 군대"로 일반화(공성=vs 수비대, 야전=vs 부대 재사용),
② check_interceptions 빈 훅, ③ ActiveOperation.id. 증분 2에서 야전은 이 위에 얹기만 하면 됨.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import ActiveOperation, Battle, Domestic, GameState, Scheme

# ======================= 캘리브레이션 손잡이 (플레이테스트로 튜닝) =======================
FACTION_SPEED = {"오": 1.25}  # 진군 진행도/월(세력별). 미지정=DEFAULT_SPEED. 오=수전 기동(장강 고속). [[DISCUSSION#9-9]]
DEFAULT_SPEED = 1.0          # 기본 진군 속도. 이동 threshold=거리(개월)라 1.0이면 거리만큼 걸려 도착.
RIVER_CROSS_PENALTY = 1      # 강 구간 도하 지연(개월). 위·촉만 — 오(수전)는 면제.
PREP_RATE = 0.2             # 조기도착 잉여(개월) → 첫 교전 우세도 보정 환산.
PREP_CAP = 0.10             # prep 보정 상한(결정론 코어 지배 유지, STRATEGY_MODIFIER_BOUND 0.15보다 작게).
FACTION_NAVAL = {"오": 2, "위": 1, "촉": 1}  # 증분2 야전 수전 보정용 예약. 증분1 이동엔 미사용.
GENERAL_SCALE = 500         # 장수 보정 스케일: 병력배수 = 1 + sum(무력)/SCALE
WALL_DEFENSE = 2500         # 성벽 레벨당 수비 병력환산 보너스(공격 우세 시 함락 가능하게 튜닝)
ATTRITION_RATE = 0.10       # 교전 월 손실 계수(상대 전투력 대비)
SIEGE_RATE = 3              # 교전 우세도(전력비-1) → 진행도/월
SIEGE_BASE = 2              # 교전 threshold = SIEGE_BASE + 성벽레벨
DOMESTIC_GAIN = 2           # 내정: 금 1 지출 → 식량/병력 2 (모병·식량증산)

SCENARIO_PATH = Path(__file__).resolve().parent.parent / "data" / "scenario.json"


# ======================= 로드 =======================
def load_scenario(path: Path | str = SCENARIO_PATH) -> GameState:
    """시나리오 JSON → 검증된 GameState. (내가 짠 데이터지만 오타 잡게 Pydantic 통과.)"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return GameState.model_validate(raw)


# ======================= 지형 =======================
def _is_river(state: GameState, a: str, b: str) -> bool:
    """구간 (a,b)가 강인가(무순서). 도하 지연·증분2 수전 보정 판정."""
    pair = {a, b}
    return any(pair == set(e) for e in state.river_edges)


# ======================= 전투 (seam ①: 군대 vs 군대 일반화) =======================
def _power(troops: int, generals: list[str], state: GameState) -> float:
    """부대 전투력 = 병력 × (1 + 장수 무력합/스케일). 공성·야전 공통."""
    bonus = sum(state.generals[g].might for g in generals if g in state.generals)
    return troops * (1 + bonus / GENERAL_SCALE)


def _combat_round(
    atk_troops: int, atk_generals: list[str],
    def_troops: int, def_generals: list[str],
    state: GameState, wall: int = 0,
) -> tuple[int, int, float]:
    """한 달치 교전 결과: (공격 손실, 방어 손실, 우세도). 공성·야전 재사용(§9-9 seam①).

    wall은 방어측 성벽 보너스(야전은 0). 우세도>0이면 공격이 유리해 공성 진행도 누적.
    """
    atk = _power(atk_troops, atk_generals, state)
    dfn = _power(def_troops, def_generals, state) + wall * WALL_DEFENSE
    atk_loss = min(atk_troops, round(dfn * ATTRITION_RATE))
    def_loss = min(def_troops, round(atk * ATTRITION_RATE))
    dominance = (atk / dfn - 1) if dfn > 0 else 999.0
    return atk_loss, def_loss, dominance


# ======================= 작전 개시 (검증=평가표면) =======================
def start_operation(state: GameState, action: Battle) -> ActiveOperation | None:
    """공성 진군 개시. 출발도시 보유와 대조 → 위반은 클램프+로깅(A·B층 카운트 표면)."""
    origin = state.cities.get(action.origin)
    if origin is None:
        state.history.append(f"[기각] 출발도시 '{action.origin}' 없음")
        return None
    faction = origin.owner
    if action.target not in state.distances.get(action.origin, {}):
        state.history.append(
            f"[위반] {faction} {action.origin}→{action.target} 비인접 진군(순간이동 시도) → 기각")
        return None

    committed = min(action.troops, origin.troops)
    if action.troops > origin.troops:
        state.history.append(
            f"[환각] {faction} 과투입 {action.troops}>{origin.troops} 보유 → {committed}로 클램프")
    if committed <= 0:
        state.history.append(f"[기각] {action.origin} 가용 병력 0")
        return None

    valid = [g for g in action.generals if g in origin.generals]
    dropped = [g for g in action.generals if g not in origin.generals]
    if dropped:
        state.history.append(f"[환각] {faction} 미보유/타지 장수 {dropped} 제외")

    # 출발도시에서 실제 차감(병력·장수가 행군해 나감)
    origin.troops -= committed
    for g in valid:
        origin.generals.remove(g)

    dist = float(state.distances[action.origin][action.target])
    river = _is_river(state, action.origin, action.target)
    if river and faction != "오":          # 위·촉 도하 지연(오는 수전이라 면제)
        dist += RIVER_CROSS_PENALTY
    op = ActiveOperation(
        id=state.next_op_id, faction=faction, action=action,
        stage="이동", progress=0, threshold=dist,
        committed_troops=committed, committed_generals=valid,
    )
    state.next_op_id += 1
    state.operations.append(op)
    tag = "·도하" if river else ""
    state.history.append(
        f"[작전{op.id}] {faction} {action.origin}→{action.target} 진군 개시(거리 {dist:g}개월{tag}, 병력 {committed})")
    return op


# ======================= 내정 (즉시 해소) =======================
def apply_domestic(state: GameState, action: Domestic) -> None:
    city = state.cities.get(action.city)
    if city is None:
        state.history.append(f"[기각] 내정 도시 '{action.city}' 없음")
        return
    spend = min(action.gold_spent, city.gold)
    city.gold -= spend
    if action.item == "식량증산":
        city.food += spend * DOMESTIC_GAIN
    elif action.item == "모병":
        city.troops += spend * DOMESTIC_GAIN
    elif action.item == "성벽보수":
        city.wall += max(1, spend // 3000)
    elif action.item == "민심회복":
        f = state.factions.get(city.owner)
        if f:  # 민심은 하드 바운드 0~100 → 엔진에서 명시 클램프(모델 assign은 재검증 안 함)
            f.morale = max(0, min(100, f.morale + spend // 500))
    state.history.append(f"[내정] {city.owner} {action.city} {action.item}(금 {spend})")


# ======================= 작전 진행 (이동 → 교전 → 해소) =======================
def _advance_operation(state: GameState, op: ActiveOperation) -> None:
    city = state.cities.get(op.action.target)
    if city is None:
        state.history.append(f"[작전{op.id}] 대상 도시 소멸 → 취소")
        state.operations.remove(op)
        return

    if op.stage == "이동":
        op.progress += FACTION_SPEED.get(op.faction, DEFAULT_SPEED)
        if op.progress >= op.threshold:
            op.prep = op.progress - op.threshold   # 조기도착 잉여(오만 >0) → 교전 선취(토루)
            op.stage = "교전"
            op.progress = 0
            op.threshold = SIEGE_BASE + city.wall
            prep_note = f", 준비 {op.prep:.2f}" if op.prep > 0 else ""
            state.history.append(f"[작전{op.id}] {op.faction} 부대 {city.name} 도착 → 교전 개시{prep_note}")
        return

    # 교전: 군대 vs 성 수비대 (seam① 함수 재사용, wall 보너스)
    atk_loss, def_loss, dominance = _combat_round(
        op.committed_troops, op.committed_generals,
        city.troops, city.generals, state, wall=city.wall)
    if op.prep > 0:                                # 조기도착 준비이점: 캡 씌운 결정론 1회 보정
        dominance += min(PREP_CAP, op.prep * PREP_RATE)
        op.prep = 0
    op.committed_troops -= atk_loss
    city.troops -= def_loss
    op.progress += max(0, round(dominance * SIEGE_RATE))

    if city.troops <= 0 or op.progress >= op.threshold:
        _capture_city(state, op, city)
    elif op.committed_troops <= 0:
        state.history.append(f"[작전{op.id}] {op.faction} 공격 붕괴 → 격퇴")
        state.operations.remove(op)


def _capture_city(state: GameState, op: ActiveOperation, city) -> None:
    """함락: 소유 이전 + 약탈(자원은 그대로 승자 소유 = 보존, 복사 없음) + 참수 게이트."""
    loser, winner = city.owner, op.faction
    city.owner = winner
    city.troops = op.committed_troops          # 생존 공격군이 주둔
    city.generals = list(op.committed_generals)  # 공격 장수가 접수 (식량·금은 도시에 그대로 = 약탈)
    state.history.append(f"[작전{op.id}] {winner}, {city.name} 함락 (구 {loser})")
    if op in state.operations:
        state.operations.remove(op)

    lf = state.factions.get(loser)
    if lf is None or loser == "중립":
        return
    remaining = [n for n, c in state.cities.items() if c.owner == loser]
    if not remaining:  # 마지막 거점 함락 → 군주 도주 불가 → 참수/포획 (하드 게이트 §9-4)
        lf.alive = False
        state.factions[winner].prisoners.append(lf.ruler)
        state.history.append(f"⚔ {loser} 군주 {lf.ruler} 포획 — {loser} 멸망")
    elif lf.capital == city.name:  # 수도만 함락, 잔여 거점 있음 → 천도
        lf.capital = remaining[0]
        state.history.append(f"{loser} 수도 함락 → {remaining[0]}(으)로 천도")


# ======================= 요격 훅 (seam ②: 증분1 no-op) =======================
def check_interceptions(state: GameState) -> None:
    """증분 2에서 이동중 작전 요격(야전) 로직을 여기 채운다. 증분1은 아무것도 안 함."""
    return


# ======================= 승리 판정 =======================
def check_victory(state: GameState) -> None:
    living = [n for n, f in state.factions.items() if f.alive]
    if len(living) == 1:
        state.winner = living[0]
        state.history.append(f"★ {living[0]} 천하 제패 (참수 승리)")
        return
    owners = {c.owner for c in state.cities.values()}
    non_neutral = owners - {"중립"}
    if "중립" not in owners and len(non_neutral) == 1:  # 천하통일(전 도시 단일 세력)
        state.winner = next(iter(non_neutral))
        state.history.append(f"★ {state.winner} 천하통일")


# ======================= 턴 오케스트레이션 =======================
def _dispatch(state: GameState, action) -> None:
    if isinstance(action, Domestic):
        apply_domestic(state, action)
    elif isinstance(action, Battle):
        if action.mode == "공성":
            start_operation(state, action)
        else:
            state.history.append(f"[증분2] 야전 미구현 → 무시 ({action.origin}→{action.target})")
    elif isinstance(action, Scheme):
        state.history.append(f"[증분2] 계략({action.scheme_type}) 미구현 → 무시")


def advance_turn(state: GameState, actions: list) -> None:
    """한 달 진행: 의도 반영(개시/내정) → 요격훅 → 작전 진행 → 승리판정 → 시간."""
    for a in actions:
        _dispatch(state, a)
    check_interceptions(state)                       # seam② 빈 훅
    for op in list(state.operations):                # 복사본 순회(해소 시 remove 안전)
        _advance_operation(state, op)
    check_victory(state)
    state.month += 1
    if state.month > 12:
        state.month = 1
        state.year += 1


def demo() -> None:
    """엔진 수동 sanity — 시나리오 로드 후 스크립트 한 판. `python -m src.engine`."""
    state = load_scenario()
    print(f"로드: 도시 {len(state.cities)}개, 세력 {len(state.factions)}개, 장수 {len(state.generals)}명")
    # 오가 장강 건너 하비(위) 강습. river 구간(하비-건업)이나 오는 도하 면제 + 속도 1.25 → 조기도착 prep.
    atk = Battle(kind="전투", mode="공성", origin="건업", target="하비",
                 troops=25000, generals=["육손", "태사자"], strategy="장강 건너 하비 기습")
    advance_turn(state, [atk])
    for _ in range(15):
        if state.winner or not state.operations:
            break
        advance_turn(state, [])
    print("\n".join(state.history[-12:]))
    print(f"\n하비 소유: {state.cities['하비'].owner} / 승자: {state.winner}")


if __name__ == "__main__":
    demo()
