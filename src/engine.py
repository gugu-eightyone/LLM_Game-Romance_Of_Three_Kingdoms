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
from typing import NamedTuple

from .models import ActiveOperation, Battle, Domestic, GameState, Scheme, Transfer

# 캘리브레이션 상수 = config.py로 분리(튜닝 손잡이 한 곳). 여긴 로직만.
from .config import (
    ATTRITION_RATE, CAPTURE_FLOOR, DEFAULT_SPEED, DOMESTIC_GAIN, ESCORT_MIN_TROOPS,
    FACTION_SPEED, FIELD_CAPTURE_BASE, FIELD_RETREAT_LOSS, GENERAL_SCALE,
    MAX_ORDERS_PER_TURN, MORALE_COMBAT_BAND, PREP_CAP, PREP_RATE,
    RIVER_CROSS_PENALTY, ROUT_MORALE_THRESHOLD, SIEGE_BASE, SIEGE_RATE,
    UNIT_MORALE_COMBAT_DROP, WALL_DEFENSE,
)

SCENARIO_PATH = Path(__file__).resolve().parent.parent / "data" / "scenario.json"


# ======================= 로드 =======================
def load_scenario(path: Path | str = SCENARIO_PATH) -> GameState:
    """시나리오 JSON → 검증된 GameState. (내가 짠 데이터지만 오타 잡게 Pydantic 통과.)"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    state = GameState.model_validate(raw)
    for c in state.cities.values():                  # 로스터 소속 = 시작 주둔 도시 소유주(등용 시 갱신)
        for g in c.generals:
            if g in state.generals and not state.generals[g].faction:
                state.generals[g].faction = c.owner
    return state


# ======================= 지형 =======================
def _is_river(state: GameState, a: str, b: str) -> bool:
    """구간 (a,b)가 강인가(무순서). 도하 지연·증분2 수전 보정 판정."""
    pair = {a, b}
    return any(pair == set(e) for e in state.river_edges)


# ======================= 전투 (seam ①: 군대 vs 군대 일반화) =======================
def _power(state: GameState, troops: int, generals: list[str], morale: int = 50) -> float:
    """부대 전투력 = 병력 × (1 + 최고통솔/스케일) × 사기배수. 공성·야전 공통.

    지휘관(최고 통솔) 1명만 반영 — 장수 수 스택은 무보상(부장 시스템 없음, 위 깊이 우위 억제).
    군 전투는 통솔만 읽음(무력=일기토·지력=계략은 각자 서브시스템). [[DISCUSSION#9-15]]·[[DISCUSSION#9-16]]
    사기배수 = 1+(morale−50)/50×BAND (m50=1.0 → 기본상태 무영향). [[DISCUSSION#9-10]]
    """
    cmds = [state.generals[g].command for g in generals if g in state.generals]
    bonus = max(cmds) if cmds else 0
    morale_factor = 1 + (morale - 50) / 50 * MORALE_COMBAT_BAND
    return troops * (1 + bonus / GENERAL_SCALE) * morale_factor


class Force(NamedTuple):
    """전투 한 진영의 입력(대칭). 성=wall 보유·야전=wall 0. morale=전투력 배수. 나중 judge 넛지도 여기 필드 추가."""
    troops: int
    generals: list[str]
    wall: int = 0
    morale: int = 50


def _combat_round(state: GameState, a: Force, b: Force) -> tuple[int, int, float]:
    """한 달치 교전 결과: (a 손실, b 손실, a우세도). 공성·야전 공통(§9-9 seam①).

    완전 대칭 — 역할(공/수)은 호출자가 우세도 부호로 해석(공성: a=공격, 우세도>0이면 진행도 누적).
    wall은 그 진영 축성 보너스(야전=0). morale=전투력 배수(미지정 50=중립 → 기존 무변).
    """
    ap = _power(state, a.troops, a.generals, a.morale) + a.wall * WALL_DEFENSE
    bp = _power(state, b.troops, b.generals, b.morale) + b.wall * WALL_DEFENSE
    a_loss = min(a.troops, round(bp * ATTRITION_RATE))
    b_loss = min(b.troops, round(ap * ATTRITION_RATE))
    dominance = (ap / bp - 1) if bp > 0 else 999.0
    return a_loss, b_loss, dominance


# ======================= 작전 개시 (검증=평가표면) =======================
def start_operation(state: GameState, action: Battle,
                    actor: str | None = None) -> ActiveOperation | None:
    """공성 진군 개시. 출발도시 보유와 대조 → 위반은 클램프+로깅(A·B층 카운트 표면).

    actor: 이 행동을 낸 세력. 주면 남의 도시에서 출병시키는 월권을 기각(LLM 경로).
    """
    origin = state.cities.get(action.origin)
    if origin is None:
        state.history.append(f"[기각] 출발도시 '{action.origin}' 없음")
        return None
    faction = origin.owner
    if actor is not None and faction != actor:
        state.history.append(
            f"[위반] {actor}가 남의 도시 '{action.origin}'({faction})에서 출병 시도 → 기각")
        return None
    if action.target not in state.distances.get(action.origin, {}):
        state.history.append(
            f"[위반] {faction} {action.origin}→{action.target} 비인접 진군(순간이동 시도) → 기각")
        return None

    # 공성은 남의 도시에만. 야전은 target이 "방면"이라 아군 도시 방향도 정당(=구원군 출격).
    if action.mode == "공성" and state.cities[action.target].owner == faction:
        state.history.append(f"[기각] {faction} 자국 도시 '{action.target}' 공성 시도")
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
    fac = state.factions.get(faction)
    op = ActiveOperation(
        id=state.next_op_id, faction=faction, action=action,
        stage="이동", progress=0, threshold=dist,
        committed_troops=committed, committed_generals=valid,
        unit_morale=fac.morale if fac else 50,       # 출전 시 전역 사기 복사→독립
    )
    state.next_op_id += 1
    state.operations.append(op)
    tag = "·도하" if river else ""
    state.history.append(
        f"[작전{op.id}] {faction} {action.origin}→{action.target} 진군 개시(거리 {dist:g}개월{tag}, 병력 {committed})")
    return op


# ======================= 내정 (즉시 해소) =======================
def apply_domestic(state: GameState, action: Domestic, actor: str | None = None) -> None:
    city = state.cities.get(action.city)
    if city is None:
        state.history.append(f"[기각] 내정 도시 '{action.city}' 없음")
        return
    if actor is not None and city.owner != actor:
        state.history.append(f"[위반] {actor}가 남의 도시 '{action.city}'({city.owner}) 내정 시도 → 기각")
        return
    spend = min(action.gold_spent, city.gold)
    city.gold -= spend
    if action.item == "식량증산":
        city.food += spend * DOMESTIC_GAIN
    elif action.item == "모병":
        city.troops += spend * DOMESTIC_GAIN
    elif action.item == "성벽보수":
        city.wall += max(1, spend // 3000)
    elif action.item == "사기진작":
        f = state.factions.get(city.owner)
        if f:  # 사기는 하드 바운드 0~100 → 엔진에서 명시 클램프(모델 assign은 재검증 안 함)
            f.morale = max(0, min(100, f.morale + spend // 500))
    state.history.append(f"[내정] {city.owner} {action.city} {action.item}(금 {spend})")


# ======================= 호송 (인접 아군 도시 간 자원 이동) =======================
def start_transfer(state: GameState, action: Transfer, actor: str | None = None) -> ActiveOperation | None:
    """호송 개시: 병사·장수·포로·금·식량을 인접 아군 도시로. 위반=기각/클램프+로깅(A·B층).

    이동은 전투 출격과 같은 작전 인프라를 탄다(강 도하 지연 포함) → 간선이 전선화되면 요격도 걸린다.
    """
    origin = state.cities.get(action.origin)
    if origin is None:
        state.history.append(f"[기각] 출발도시 '{action.origin}' 없음")
        return None
    faction = origin.owner
    if actor is not None and faction != actor:
        state.history.append(f"[위반] {actor}가 남의 도시 '{action.origin}'({faction})에서 호송 시도 → 기각")
        return None
    dest = state.cities.get(action.target)
    if dest is None or action.target not in state.distances.get(action.origin, {}):
        state.history.append(f"[위반] {faction} {action.origin}→{action.target} 비인접 호송 → 기각")
        return None
    if dest.owner != faction:
        state.history.append(f"[기각] {faction} 호송 목적지 '{action.target}'({dest.owner})는 아군 도시 아님")
        return None

    troops = max(0, min(action.troops, origin.troops))
    gold = max(0, min(action.gold, origin.gold))
    food = max(0, min(action.food, origin.food))
    if (troops, gold, food) != (action.troops, action.gold, action.food):
        state.history.append(f"[환각] {faction} 호송 적재량이 보유 초과/음수 → 클램프")
    gens = [g for g in action.generals if g in origin.generals]
    pris = [p for p in action.prisoners if p in origin.prisoners]
    dropped = [x for x in (*action.generals, *action.prisoners) if x not in (*gens, *pris)]
    if dropped:
        state.history.append(f"[환각] {faction} 호송 미보유 장수/포로 {dropped} 제외")
    if not (troops or gold or food or gens or pris):
        state.history.append(f"[기각] {faction} 빈 호송")
        return None
    if (troops or gold or food or pris) and troops < ESCORT_MIN_TROOPS:
        state.history.append(
            f"[기각] {faction} 호송 호위 부족(병력 {troops} < {ESCORT_MIN_TROOPS}) — 장수 단독만 무호위 가능")
        return None

    origin.troops -= troops
    origin.gold -= gold
    origin.food -= food
    for g in gens:
        origin.generals.remove(g)
    for p in pris:
        origin.prisoners.remove(p)

    dist = float(state.distances[action.origin][action.target])
    if _is_river(state, action.origin, action.target) and faction != "오":
        dist += RIVER_CROSS_PENALTY
    fac = state.factions.get(faction)
    op = ActiveOperation(
        id=state.next_op_id, faction=faction, action=action,
        stage="이동", progress=0, threshold=dist,
        committed_troops=troops, committed_generals=gens,
        cargo_gold=gold, cargo_food=food, cargo_prisoners=pris,
        unit_morale=fac.morale if fac else 50,
    )
    state.next_op_id += 1
    state.operations.append(op)
    load = "·".join(x for x in (
        f"병{troops}" if troops else "", f"금{gold}" if gold else "", f"식{food}" if food else "",
        f"장수 {','.join(gens)}" if gens else "", f"포로 {','.join(pris)}" if pris else "") if x)
    state.history.append(
        f"[작전{op.id}] {faction} 호송 {action.origin}→{action.target} ({load}, 거리 {dist:g}개월)")
    return op


# ======================= 이동 (마일스톤=암묵적, progress로 파생) =======================
def _advance_movement(state: GameState) -> None:
    """이동중 작전 진행. 필드 교전 중(고정)이거나 야전 교전 마친(has_fought) op는 안 움직임.

    마일스톤은 노드로 만들지 않음 — 위치=`(origin,target,progress)`. 충돌은 `_resolve_combat`이 술어로 판정.
    """
    engaged = _field_engaged_ids(state)
    for op in list(state.operations):
        if op.stage != "이동" or op.id in engaged:
            continue
        if op.action.mode == "야전" and op.has_fought:   # 교전 마친 야전=복귀 대기(전진 안 함)
            continue
        op.progress += FACTION_SPEED.get(op.faction, DEFAULT_SPEED)
        if op.progress >= op.threshold:
            _arrive(state, op)


def _arrive(state: GameState, op: ActiveOperation) -> None:
    """작전이 목표 도시 도착. 공성=교전 개시(수비대). 야전=적 공성 op 있으면 교전, 없으면 출격 종료."""
    city = state.cities.get(op.action.target)
    if city is None:
        state.history.append(f"[작전{op.id}] 대상 도시 소멸 → 취소")
        state.operations.remove(op)
        return
    if op.action.mode == "호송":
        if city.owner != op.faction:                  # 이동 중 목적지 함락 → 회군(화물째)
            _return_home(state, op, "호송 취소(목적지 상실)")
            return
        city.troops += op.committed_troops
        city.generals.extend(op.committed_generals)
        city.prisoners.extend(op.cargo_prisoners)
        city.gold += op.cargo_gold
        city.food += op.cargo_food
        state.history.append(f"[작전{op.id}] {op.faction} 호송 {city.name} 도착")
        state.operations.remove(op)
        return
    if op.action.mode == "공성":
        op.prep = op.progress - op.threshold          # 조기도착 잉여(오만 >0) → 교전 선취(토루)
        op.stage = "교전"
        op.progress = 0
        op.threshold = SIEGE_BASE + city.wall
        prep_note = f", 준비 {op.prep:.2f}" if op.prep > 0 else ""
        state.history.append(f"[작전{op.id}] {op.faction} 부대 {city.name} 도착 → 공성 개시{prep_note}")
    else:                                             # 야전: 그 도시 공성중인 적 있으면 구원 교전
        besiegers = [o for o in state.operations
                     if o.faction != op.faction and o.stage == "교전" and o.action.target == op.action.target]
        if besiegers:
            op.stage = "교전"                         # 도시서 야전 교전(수비대 안 침, 적 op만)
            state.history.append(f"[작전{op.id}] {op.faction} 구원군 {city.name} 도착 → 야전 교전")
        else:
            _return_home(state, op, "출격 종료(대상 없음)")


def _siege_round(state: GameState, op: ActiveOperation) -> None:
    """공성 교전 1라운드: 군대 vs 성 수비대(seam① 재사용, wall 보너스). 돌파 시 함락.

    공격=부대 사기(op.unit_morale), 수비=전역 사기(수성=작전 아님 → 세력값). 전멸/퇴각은 `_resolve_op_end`.
    """
    city = state.cities.get(op.action.target)
    if city is None:
        state.operations.remove(op)
        return
    dfac = state.factions.get(city.owner)
    atk_loss, def_loss, dominance = _combat_round(
        state,
        Force(op.committed_troops, op.committed_generals, morale=op.unit_morale),
        Force(city.troops, city.generals, wall=city.wall, morale=dfac.morale if dfac else 50))
    if op.prep > 0:                                   # 조기도착 준비이점: 캡 씌운 결정론 1회 보정
        dominance += min(PREP_CAP, op.prep * PREP_RATE)
        op.prep = 0
    op.committed_troops -= atk_loss
    op.unit_morale = max(0, op.unit_morale - UNIT_MORALE_COMBAT_DROP)
    city.troops -= def_loss
    op.progress += max(0, round(dominance * SIEGE_RATE))
    if op.committed_troops > 0 and (city.troops <= 0 or op.progress >= op.threshold):
        _capture_city(state, op, city)


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


# ======================= 야전 (지속 전투: 요격·구원군, 마일스톤=암묵) =======================
def _field_engagements(state: GameState) -> list[tuple[ActiveOperation, ActiveOperation]]:
    """현재 위치에서 교전 중인 대립 작전 쌍(야전). 위치=술어(저장 안 함, Option A).

    ① 같은 간선 반대방향 이동 + progress합 ≥ 거리 → 도로에서 교차(마일스톤 지점).
    ② 같은 도시서 둘 다 교전(공성 op ↔ 그 도시 도착한 구원 op).
    """
    ops = list(state.operations)
    pairs = []
    for i, a in enumerate(ops):
        for b in ops[i + 1:]:
            if a.faction == b.faction:
                continue
            if (a.stage == "이동" and b.stage == "이동"
                    and a.action.origin == b.action.target and a.action.target == b.action.origin):
                d = state.distances.get(a.action.origin, {}).get(a.action.target)
                if d and (a.progress + b.progress) >= d:
                    pairs.append((a, b))
                    continue
            if a.stage == "교전" and b.stage == "교전" and a.action.target == b.action.target:
                pairs.append((a, b))
    return pairs


def _field_engaged_ids(state: GameState) -> set[int]:
    return {op.id for pair in _field_engagements(state) for op in pair}


def _field_round(state: GameState, a: ActiveOperation, b: ActiveOperation) -> None:
    """야전 1라운드(부대 vs 부대, wall=0, 순수 대칭). 협공은 '두 번 맞음'으로 자연 발생(보너스 없음)."""
    a_loss, b_loss, _ = _combat_round(
        state,
        Force(a.committed_troops, a.committed_generals, morale=a.unit_morale),
        Force(b.committed_troops, b.committed_generals, morale=b.unit_morale))
    a.committed_troops -= a_loss
    b.committed_troops -= b_loss
    a.unit_morale = max(0, a.unit_morale - UNIT_MORALE_COMBAT_DROP)
    b.unit_morale = max(0, b.unit_morale - UNIT_MORALE_COMBAT_DROP)
    a.has_fought = b.has_fought = True
    state.history.append(
        f"[야전] 작전{a.id}({a.faction}) ↔ 작전{b.id}({b.faction}): -{a_loss}/-{b_loss}")


def _resolve_combat(state: GameState) -> None:
    """한 턴 전투 해소: 야전(필드 쌍) → 공성(수비대) → 종료(전멸/퇴각/포로) → 야전 승자 복귀."""
    pairs = _field_engagements(state)
    n_opp: dict[int, int] = {}
    for a, b in pairs:
        _field_round(state, a, b)
        n_opp[a.id] = n_opp.get(a.id, 0) + 1
        n_opp[b.id] = n_opp.get(b.id, 0) + 1
    for op in list(state.operations):                # 공성 라운드(필드 피해 반영된 병력으로 → 협공 자연 발생)
        if op.action.mode == "공성" and op.stage == "교전":
            _siege_round(state, op)
    for op in list(state.operations):                # 종료 판정
        _resolve_op_end(state, op, n_opp.get(op.id, 0))
    engaged = {op.id for pair in pairs for op in pair}
    for op in list(state.operations):                # 야전 승자(교전 마치고 상대 소멸) → 자동 복귀(서브초이스2)
        if (op.action.mode == "야전" and op.has_fought
                and op.id not in engaged and op.committed_troops > 0):
            _return_home(state, op, "요격 완료")


def _resolve_op_end(state: GameState, op: ActiveOperation, n_field: int) -> None:
    """전멸(야전 피해→포로 / 공성 붕괴→격퇴) + 확률적 강제 퇴각(사기 붕괴)."""
    if op not in state.operations:                   # 이미 함락 등으로 해소
        return
    if op.committed_troops <= 0:
        if n_field > 0:
            _destroy_field_op(state, op, n_field)    # 야전 전멸 → 장수 포획(base×부대수)·화물 노획
        elif op.action.mode != "호송":               # 장수 단독 호송(병력 0)은 정상 → 계속 간다
            _return_home(state, op, "격퇴" if op.action.mode == "공성" else "무위", troops=False)
        return
    fighting = op.stage == "교전" or n_field > 0
    if fighting and op.unit_morale < ROUT_MORALE_THRESHOLD:
        deficit = ROUT_MORALE_THRESHOLD - op.unit_morale
        if state.rng.random() < (deficit / ROUT_MORALE_THRESHOLD) ** 2:
            _retreat_op(state, op)


# ---- 작전 해소 처분(복귀/격퇴/퇴각/야전포로) ----
def _home_city(state: GameState, op: ActiveOperation) -> str | None:
    """복귀·퇴각 목적지: 출발도시(아군이면) → 현 위치 인접 아군 도시 → 없으면 None(고립)."""
    o = state.cities.get(op.action.origin)
    if o and o.owner == op.faction:
        return op.action.origin
    for node in (op.action.target, op.action.origin):
        for nb in state.distances.get(node, {}):
            c = state.cities.get(nb)
            if c and c.owner == op.faction:
                return nb
    return None


def _nearest_enemy_city(state: GameState, op: ActiveOperation) -> str | None:
    """야전 포로 호송지: 현 위치 인접 적(비중립) 도시."""
    for node in (op.action.target, op.action.origin):
        c = state.cities.get(node)
        if c and c.owner not in (op.faction, "중립"):
            return node
        for nb in state.distances.get(node, {}):
            c2 = state.cities.get(nb)
            if c2 and c2.owner not in (op.faction, "중립"):
                return nb
    return None


def _nearest_city_of(state: GameState, op: ActiveOperation, faction: str) -> str | None:
    """현 위치(간선 양끝) 기준 그 세력 소유 최근접 도시. 해방 포로 복귀지 판정."""
    if not faction:
        return None
    for node in (op.action.target, op.action.origin):
        for name in (node, *state.distances.get(node, {})):
            c = state.cities.get(name)
            if c and c.owner == faction:
                return name
    return None


def _return_home(state: GameState, op: ActiveOperation, reason: str, troops: bool = True) -> None:
    """출격/작전 종료 → 생존군·장수 아군 도시 복귀. 고립(복귀지 없음)이면 장수 소실."""
    dest = _home_city(state, op)
    if dest:
        c = state.cities[dest]
        if troops:
            c.troops += max(0, op.committed_troops)
        c.generals.extend(op.committed_generals)
        c.gold += op.cargo_gold                       # 호송 화물도 함께 귀환(전투 op는 전부 0)
        c.food += op.cargo_food
        c.prisoners.extend(op.cargo_prisoners)
    state.history.append(f"[작전{op.id}] {op.faction} {reason} → {dest or '복귀 실패(고립)'}")
    if op in state.operations:
        state.operations.remove(op)


def _retreat_op(state: GameState, op: ActiveOperation) -> None:
    """확률적 강제 퇴각: 추가 손실 후 아군 도시로 즉시 귀환(간선서 제거=재충돌 방지).

    복귀지 없으면(적진 고립) 퇴각 불가 → 계속 싸움 → 다음 전멸 시 야전 포로(포위의 대가).
    """
    dest = _home_city(state, op)
    if dest is None:
        return
    op.committed_troops -= round(op.committed_troops * FIELD_RETREAT_LOSS)
    c = state.cities[dest]
    c.troops += max(0, op.committed_troops)
    c.generals.extend(op.committed_generals)
    c.gold += op.cargo_gold                           # 호송 화물도 함께 퇴각(전투 op는 전부 0)
    c.food += op.cargo_food
    c.prisoners.extend(op.cargo_prisoners)
    state.history.append(f"[퇴각] {op.faction} 작전{op.id} 사기 붕괴(사기 {op.unit_morale}) → {dest}")
    state.operations.remove(op)


def _take_prisoner(state: GameState, holding_city: str, g: str) -> bool:
    """장수 g를 holding_city 수감. 군주면 is_ruler 해제 + True 반환(승계 트리거)."""
    state.cities[holding_city].prisoners.append(g)
    gen = state.generals.get(g)
    if gen is not None and gen.is_ruler:
        gen.is_ruler = False
        return True
    return False


def _destroy_field_op(state: GameState, op: ActiveOperation, n_field: int) -> None:
    """야전 전멸: 장수 포획확률 = base × 교전 적 부대 수. 포획=최근접 적 도시 호송(탈영 없음), 실패=아군 복귀."""
    prob = min(1.0, FIELD_CAPTURE_BASE * n_field)
    jail = _nearest_enemy_city(state, op)
    home = _home_city(state, op)
    ruler_lost = False
    for g in list(op.committed_generals):
        if jail and state.rng.random() < prob:
            if _take_prisoner(state, jail, g):
                ruler_lost = True
            state.history.append(f"  ↳ {g} 야전 포로 → {jail}")
        elif home:
            state.cities[home].generals.append(g)     # 포획 실패 → 아군 복귀
    if jail and (op.cargo_gold or op.cargo_food):     # 호송 화물 = 요격측 노획
        state.cities[jail].gold += op.cargo_gold
        state.cities[jail].food += op.cargo_food
        state.history.append(f"  ↳ 화물 노획(금{op.cargo_gold}·식{op.cargo_food}) → {jail}")
    for p in op.cargo_prisoners:                      # 호송 중 포로 = 해방 → 원 세력 최근접 도시
        pf = state.generals[p].faction if p in state.generals else ""
        free = _nearest_city_of(state, op, pf)
        if free:
            state.cities[free].generals.append(p)
            state.history.append(f"  ↳ 포로 {p} 해방 → {free}")
        elif jail:                                    # 원 세력 도시 없음(소멸 등) → 재수감
            state.cities[jail].prisoners.append(p)
    state.history.append(f"[야전 전멸] {op.faction} 작전{op.id} 궤멸")
    state.operations.remove(op)
    if ruler_lost and state.factions.get(op.faction) and state.factions[op.faction].alive:
        _succeed_ruler(state, op.faction)


# ======================= 승리 판정 =======================
def check_victory(state: GameState) -> None:
    """승리 = 천하통일(전 도시 단일 세력) 단일 조건. 참수 즉시승리 폐기(§9-16). 세력 소멸=도시0."""
    owners = {c.owner for c in state.cities.values()}
    non_neutral = owners - {"중립"}
    if "중립" not in owners and len(non_neutral) == 1:
        state.winner = next(iter(non_neutral))
        state.history.append(f"★ {state.winner} 천하통일")


# ======================= 턴 오케스트레이션 =======================
def _dispatch(state: GameState, action, actor: str | None = None) -> None:
    if isinstance(action, Domestic):
        apply_domestic(state, action, actor)
    elif isinstance(action, Battle):
        start_operation(state, action, actor)        # 공성·야전 모두 진군 작전으로 개시
    elif isinstance(action, Transfer):
        start_transfer(state, action, actor)
    elif isinstance(action, Scheme):
        state.history.append(f"[증분2] 계략({action.scheme_type}) 미구현 → 무시")


def advance_turn(state: GameState, actions: list | dict) -> None:
    """한 달 진행: 개시/내정 → 이동(마일스톤) → 전투 해소(야전·공성·퇴각·포로) → 승리 → 시간.

    actions: `{세력: Action | list[Action]}` dict면 행위자 소유권 검증 + 명령 상한(LLM 경로),
             그냥 list면 검증 생략(스크립트 데모·테스트가 상태를 직접 짜는 경우).
    명령은 적힌 순서대로 즉시 처리(모병 → 그 병력으로 같은 턴 출격 가능).
    """
    if isinstance(actions, dict):
        items = []
        for actor, acts in actions.items():
            acts = list(acts) if isinstance(acts, list) else [acts]
            if len(acts) > MAX_ORDERS_PER_TURN:
                state.history.append(
                    f"[환각] {actor} 명령 {len(acts)}건 > 상한 {MAX_ORDERS_PER_TURN} → 초과분 무시")
                acts = acts[:MAX_ORDERS_PER_TURN]
            items.extend((actor, a) for a in acts)
    else:
        items = [(None, a) for a in actions]
    for actor, a in items:
        _dispatch(state, a, actor)
    _advance_movement(state)                          # 이동중 작전 진행(필드 교전=고정)
    _resolve_combat(state)                            # 야전 쌍 → 공성 → 종료 판정
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

    # 도로 요격 데모: 위 장안→한중 공성 진군(거리2)을, 촉 한중→장안 야전 출격으로 길목 요격.
    print("\n--- 도로 요격 데모 (장안↔한중 거리2) ---")
    s2 = load_scenario()
    h0 = len(s2.history)
    advance_turn(s2, [
        Battle(kind="전투", mode="공성", origin="장안", target="한중", troops=20000, generals=["하후연"],
               strategy="한중 공략"),
        Battle(kind="전투", mode="야전", origin="한중", target="장안", troops=18000, generals=["마초"],
               strategy="길목에서 요격"),
    ])
    for _ in range(6):
        if not s2.operations:
            break
        advance_turn(s2, [])
    print("\n".join(s2.history[h0:][:10]))


if __name__ == "__main__":
    demo()
