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

# 캘리브레이션 상수 = config.py로 분리(튜닝 손잡이 한 곳). 여긴 로직만.
from .config import (
    ATTRITION_RATE, CAPTURE_FLOOR, DEFAULT_SPEED, DOMESTIC_GAIN, FACTION_SPEED,
    GENERAL_SCALE, PREP_CAP, PREP_RATE, RIVER_CROSS_PENALTY, SIEGE_BASE,
    SIEGE_RATE, WALL_DEFENSE,
)

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
    """부대 전투력 = 병력 × (1 + 최고통솔/스케일). 공성·야전 공통.

    지휘관(최고 통솔) 1명만 반영 — 장수 수 스택은 무보상(부장 시스템 없음, 위 깊이 우위 억제).
    군 전투는 통솔만 읽음(무력=일기토·지력=계략은 각자 서브시스템). [[DISCUSSION#9-15]]·[[DISCUSSION#9-16]]
    """
    cmds = [state.generals[g].command for g in generals if g in state.generals]
    bonus = max(cmds) if cmds else 0
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
    """함락: 소유 이전 + 약탈 + 방어 장수 탈출/포로 판정(포위도·seeded RNG) + 군주 자동 승계.

    포위도 = 1 − (패자 소유 인접 도시 / 총 인접). 공격 출발지가 아군 아니라 항상 >0.
    포획확률 = max(CAPTURE_FLOOR, 포위도²)(볼록: 제대로 에워싸야 급증). 완전 포위=탈출로0=확정.
    방어 병사는 증발(승자 생존군이 주둔), 장수만 탈출/포로. 군주 포획=승계 트리거(멸망은 도시0). [[DISCUSSION#9-10]]·[[DISCUSSION#9-16]]
    """
    loser, winner = city.owner, op.faction
    defenders = list(city.generals)                 # 접수 전 방어 장수(군주 포함 가능)

    neighbors = list(state.distances.get(city.name, {}))
    total = len(neighbors) or 1
    escape_dests = sorted(n for n in neighbors
                          if n in state.cities and state.cities[n].owner == loser)
    encircle = 1 - len(escape_dests) / total
    capture_prob = max(CAPTURE_FLOOR, encircle ** 2)

    # 도시 접수(금·식량=도시에 그대로=약탈, 병사=생존 공격군 주둔, 방어 병사 증발)
    city.owner = winner
    city.troops = op.committed_troops
    city.generals = list(op.committed_generals)
    state.history.append(f"[작전{op.id}] {winner}, {city.name} 함락 (구 {loser})")
    if op in state.operations:
        state.operations.remove(op)

    ruler_captured = False
    for g in defenders:                              # 장수별 탈출/포로 (seeded RNG)
        if escape_dests and state.rng.random() >= capture_prob:
            state.cities[escape_dests[0]].generals.append(g)
            state.history.append(f"  ↳ {g} {escape_dests[0]}(으)로 탈출")
        else:
            city.prisoners.append(g)
            state.history.append(f"  ↳ {g} 포로 (포위도 {encircle:.2f})")
            gen = state.generals.get(g)
            if gen is not None and gen.is_ruler:
                gen.is_ruler = False
                ruler_captured = True

    lf = state.factions.get(loser)
    if lf is None or loser == "중립":
        return
    remaining = [n for n, c in state.cities.items() if c.owner == loser]
    if not remaining:                                # 전 도시 상실 → 세력 소멸
        lf.alive = False
        state.history.append(f"⚔ {loser} 멸망 (전 도시 상실)")
    elif ruler_captured:                             # 군주 포획인데 도시 잔존 → 자동 승계
        _succeed_ruler(state, loser)


def _succeed_ruler(state: GameState, faction: str) -> None:
    """군주 포획 시 최고 통솔 생존 장수 자동 승계(충성도·내분 X = 스코프 크립 회피). [[DISCUSSION#9-16]]"""
    cand = [g for c in state.cities.values() if c.owner == faction for g in c.generals]
    if not cand:                                     # 승계할 장수 없음(무두 잔존 → 다음 함락서 정리)
        state.factions[faction].ruler = ""
        return
    heir = max(cand, key=lambda g: state.generals[g].command if g in state.generals else 0)
    state.generals[heir].is_ruler = True
    state.factions[faction].ruler = heir
    state.history.append(f"  ↳ {faction} {heir} 군주 승계")


# ======================= 요격 훅 (seam ②: 증분1 no-op) =======================
def check_interceptions(state: GameState) -> None:
    """증분 2에서 이동중 작전 요격(야전) 로직을 여기 채운다. 증분1은 아무것도 안 함."""
    return


# ======================= 승리 판정 =======================
def check_victory(state: GameState) -> None:
    """승리 = 천하통일(전 도시 단일 세력) 단일 조건. 참수 즉시승리 폐기(§9-16). 세력 소멸=도시0."""
    owners = {c.owner for c in state.cities.values()}
    non_neutral = owners - {"중립"}
    if "중립" not in owners and len(non_neutral) == 1:
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
