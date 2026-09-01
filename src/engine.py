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

from .models import (
    ActiveOperation, Battle, City, Diplomacy, Domestic, GameState, OpCommand, Persuade,
    Proposal, Scheme, Transfer,
)

# 캘리브레이션 상수 = config.py로 분리(튜닝 손잡이 한 곳). 여긴 로직만.
from .config import (
    ATTRITION_RATE, CAPTURE_FLOOR, CITY_INCOME_FOOD, CITY_INCOME_GOLD,
    DEFAULT_SPEED, DOMESTIC_GAIN, ESCORT_MIN_TROOPS, TROOPS_PER_FOOD,
    FACTION_SPEED, FIELD_CAPTURE_BASE, FIELD_RETREAT_LOSS, GENERAL_SCALE,
    MAX_ORDERS_PER_TURN, MORALE_CITY_LOST, MORALE_CITY_TAKEN, MORALE_COMBAT_BAND,
    MORALE_FEAST_CAP, MORALE_RULER_CAPTURED,
    PERSUADE_BASE, PERSUADE_INTEL_SCALE, PREP_CAP, PREP_RATE,
    SURRENDER_CITY_GATE, SURRENDER_CITY_WEIGHT,
    RIVER_CROSS_PENALTY, ROUT_MORALE_THRESHOLD, SIEGE_BASE, SIEGE_RATE,
    SIEGE_RETREAT_SURVIVAL, SORTIE_SLOW_CAP, UNIT_MORALE_COMBAT_DROP,
    WALL_CAPTURE_HEAL_RATIO, WALL_DEFENSE, WALL_HP_SCALE, WALL_MANNING,
    WALL_REGEN_PER_TURN, WALL_REPAIR_GOLD_PER_HP,
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


# ======================= 연혁 (주요 사건 영구 기록) =======================
def _chronicle(state: GameState, event: str) -> None:
    """주요 연혁 기록 — brief가 전량 노출(영구). 굵직한 사건만: 함락·군주 포획/승계·멸망·통일.

    LLM 요약 대신 결정론 기록(누락 0·호출 0). 원한·복수 같은 해석은 LLM 역할극 몫.
    """
    state.chronicle.append(f"{state.year}년 {state.month}월: {event}")


# ======================= 지형 =======================
def _is_river(state: GameState, a: str, b: str) -> bool:
    """구간 (a,b)가 강인가(무순서). 도하 지연·증분2 수전 보정 판정."""
    pair = {a, b}
    return any(pair == set(e) for e in state.river_edges)


# ======================= 성벽 HP (⭐2026-09-01 A안: 게이지 주인=도시) =======================
def _wall_max(city: City) -> int:
    """성벽 최대 HP = (SIEGE_BASE + 레벨) × 환산상수. 레벨은 정적(수비 보너스·정원제도 레벨 기준 유지)."""
    return (SIEGE_BASE + city.wall) * WALL_HP_SCALE


def _wall_hp(city: City) -> int:
    """현재 성벽 HP. -1(미초기화)=만액 — 구 세이브·수제 테스트 상태가 자동으로 온전한 성이 된다."""
    return _wall_max(city) if city.wall_hp < 0 else city.wall_hp


def _wall_regen(state: GameState, skip: frozenset[str] = frozenset()) -> None:
    """평시 성벽 자연회복(전간기 복구): 피침(접근 포함) 아닐 때만 월 +REGEN, 만액까지. 정상 틱=무로그(스팸).

    skip: 이번 턴 주인 바뀐 도시(전투로 밤샌 성 — 점령 응급 보수만 받고 자연회복은 다음 달부터).
    """
    for c in state.cities.values():
        if c.wall_hp < 0 or c.owner == "중립" or c.name in skip:   # 미초기화(만액)=회복할 것 없음
            continue
        m = _wall_max(c)
        if c.wall_hp < m and not _city_threats(state, c.name, c.owner):
            c.wall_hp = min(m, c.wall_hp + WALL_REGEN_PER_TURN)


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
    성벽은 정원제 — min(wall, 병력/WALL_MANNING)레벨만 위력(빈 성벽 혼자 못 싸움 → 출성 배분 리스크). [[DISCUSSION#9-20]]
    """
    ap = _power(state, a.troops, a.generals, a.morale) + min(a.wall, a.troops / WALL_MANNING) * WALL_DEFENSE
    bp = _power(state, b.troops, b.generals, b.morale) + min(b.wall, b.troops / WALL_MANNING) * WALL_DEFENSE
    a_loss = min(a.troops, round(bp * ATTRITION_RATE))
    b_loss = min(b.troops, round(ap * ATTRITION_RATE))
    dominance = (ap / bp - 1) if bp > 0 else 999.0
    return a_loss, b_loss, dominance


def _shift_morale(state: GameState, faction: str, delta: int, why: str) -> None:
    """전역 사기 이벤트 훅(§9-10 배선): 함락·상실·격퇴·군주 피랍이 사기를 움직인다. 클램프 0~100."""
    f = state.factions.get(faction)
    if f is None or delta == 0:
        return
    f.morale = max(0, min(100, f.morale + delta))
    state.history.append(f"[사기] {faction} {delta:+d} ({why}) → {f.morale}")


def allied(state: GameState, a: str, b: str) -> bool:
    """두 세력이 동맹인가. 쌍은 정렬 튜플로 정규화 저장. [[DISCUSSION#9-22]]"""
    return tuple(sorted((a, b))) in state.alliances


def _city_threats(state: GameState, city: str, owner: str) -> list[ActiveOperation]:
    """이 도시를 노리는 적 전투 작전(이동·교전 불문). decide의 피침 경보와 같은 술어 — 경보가 뜨면 출성도 성립.

    동맹 부대의 접근(구원군)은 위협이 아님.
    """
    return [o for o in state.operations
            if o.faction != owner and not allied(state, o.faction, owner)
            and o.action.mode in ("공성", "야전")
            and getattr(o.action, "target", None) == city]


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

    # 출성(태세, §9-20): origin==target 야전 = 피침 도시(적이 노리는 중, 접근 포함)에서만 성립.
    # 수비대를 분할해 성 앞에 진 침(wall 포기) — 적 도착 즉시 ②술어가 페어링(예비 출성=첫 공성 라운드 선타).
    sortie = action.mode == "야전" and action.origin == action.target
    if sortie and not _city_threats(state, action.origin, faction):
        state.history.append(f"[기각] {faction} {action.origin} 출성 — 이 도시를 노리는 적 없음(수성은 자동)")
        return None
    if not sortie and action.target not in state.distances.get(action.origin, {}):
        state.history.append(
            f"[위반] {faction} {action.origin}→{action.target} 비인접 진군(순간이동 시도) → 기각")
        return None

    # 공성은 남의 도시에만. 야전은 target이 "방면"이라 아군 도시 방향도 정당(=구원군 출격).
    if action.mode == "공성" and state.cities[action.target].owner == faction:
        state.history.append(f"[기각] {faction} 자국 도시 '{action.target}' 공성 시도")
        return None
    if action.mode == "공성" and allied(state, faction, state.cities[action.target].owner):
        state.history.append(
            f"[기각] {faction} 동맹({state.cities[action.target].owner}) 도시 '{action.target}' 공성 — 치려면 먼저 파기하라")
        return None

    # 검산 칸 대조(측정 표면): 같은 턴 앞선 모병으로 보유가 브리핑과 달라진 경우도 불일치로 찍힘 — 집계 시 감안.
    if action.origin_troops_seen >= 0 and action.origin_troops_seen != origin.troops:
        state.history.append(
            f"[검산 불일치] {faction} {action.origin} 보유 {origin.troops} ≠ 기재 {action.origin_troops_seen}")

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

    if sortie:
        dist, river = 0.0, False                      # 성 앞이라 이동 없음 → 즉시 교전
    else:
        dist = float(state.distances[action.origin][action.target])
        river = _is_river(state, action.origin, action.target)
        if river and faction != "오":      # 위·촉 도하 지연(오는 수전이라 면제)
            dist += RIVER_CROSS_PENALTY
    fac = state.factions.get(faction)
    op = ActiveOperation(
        id=state.next_op_id, faction=faction, action=action,
        stage="교전" if sortie else "이동", progress=0, threshold=dist,
        committed_troops=committed, committed_generals=valid,
        unit_morale=fac.morale if fac else 50,       # 출전 시 전역 사기 복사→독립
    )
    state.next_op_id += 1
    state.operations.append(op)
    if sortie:
        state.history.append(f"[작전{op.id}] {faction} {action.origin} 출성(성 앞 포진, 병력 {committed})")
    else:
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
    if action.item == "성벽보수":                     # ⭐HP화(2026-09-01): 보수=파손 복구(레벨 증축 아님, 만액 상한)
        heal_cap = _wall_max(city) - _wall_hp(city)
        if heal_cap <= 0:
            state.history.append(f"[기각] {city.owner} {action.city} 성벽보수 — 성벽이 온전함(파손 없음)")
            return
        heal = min(heal_cap, spend // WALL_REPAIR_GOLD_PER_HP)
        if heal <= 0:
            state.history.append(f"[기각] {city.owner} {action.city} 성벽보수 — 금 부족(HP 1당 금 {WALL_REPAIR_GOLD_PER_HP})")
            return
        cost = heal * WALL_REPAIR_GOLD_PER_HP         # 파손분만큼만 과금(초과 지출 안 받음)
        city.gold -= cost
        city.wall_hp = _wall_hp(city) + heal
        state.history.append(
            f"[내정] {city.owner} {action.city} 성벽보수 (HP +{heal} → {city.wall_hp}/{_wall_max(city)}, 금 {cost})")
        return
    city.gold -= spend
    if action.item == "식량증산":
        city.food += spend * DOMESTIC_GAIN
    elif action.item == "모병":
        city.troops += spend * DOMESTIC_GAIN
    elif action.item == "사기진작":
        f = state.factions.get(city.owner)
        if f and f.morale < MORALE_FEAST_CAP:  # 만찬 천장(⭐금 도배 방지): 그 위 사기는 승리(함락)로만
            f.morale = min(MORALE_FEAST_CAP, f.morale + spend // 500)
        elif f:
            state.history.append(f"[내정] {city.owner} 사기진작 무효 — 잔치로는 사기 {MORALE_FEAST_CAP} 이상 못 올림")
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


# ======================= 작전지시 (진행 중 작전 제어: 회군·전략변경) =======================
def apply_op_command(state: GameState, action: OpCommand, actor: str | None = None) -> None:
    """진행 중 작전에 지시. 회군=즉시 복귀(교전 중=퇴각 손실) → 같은 턴 뒤 명령으로 재출격 가능(=작전 변경).
    전략변경=전략문 교체(judge 배선 시 교전 보정 입력). 남의 작전=기각."""
    op = next((o for o in state.operations if o.id == action.op_id), None)
    if op is None:
        state.history.append(f"[기각] 작전지시 대상 작전{action.op_id} 없음")
        return
    if actor is not None and op.faction != actor:
        state.history.append(f"[위반] {actor}가 남의 작전{op.id}({op.faction})에 지시 시도 → 기각")
        return
    if action.order == "회군":
        fighting = op.stage == "교전" or op.id in _field_engaged_ids(state)
        if fighting:                                  # 싸우다 빠지는 건 공짜가 아님(강제 퇴각과 동일 손실)
            op.committed_troops -= round(op.committed_troops * FIELD_RETREAT_LOSS)
        _return_home(state, op, "회군" + ("(교전 이탈, 퇴각 손실)" if fighting else " 명령"))
    else:                                             # 전략변경
        if not hasattr(op.action, "strategy"):
            state.history.append(f"[기각] 작전{op.id}({op.action.mode})은 전략을 갖지 않음")
            return
        op.action.strategy = action.strategy
        state.history.append(f"[지시] 작전{op.id} 전략 갱신: {action.strategy}")


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
        if _join_friendly_siege(state, op, city):     # 이동 중 아군이 선점 함락/동맹 체결 → 입성/복귀
            return
        op.prep = op.progress - op.threshold          # 조기도착 잉여(오만 >0) → 교전 선취(토루)
        op.stage = "교전"
        op.progress = 0
        op.threshold = 0                              # ⭐HP화: 함락 게이지=도시의 wall_hp(진행도 폐기)
        prep_note = f", 준비 {op.prep:.2f}" if op.prep > 0 else ""
        state.history.append(f"[작전{op.id}] {op.faction} 부대 {city.name} 도착 → 공성 개시{prep_note}")
    else:                                             # 야전: 그 도시 공성중인 적 있으면 구원 교전
        besiegers = [o for o in state.operations
                     if o.faction != op.faction and not allied(state, o.faction, op.faction)
                     and o.stage == "교전" and o.action.target == op.action.target]
        if besiegers:
            op.stage = "교전"                         # 도시서 야전 교전(수비대 안 침, 적 op만)
            state.history.append(f"[작전{op.id}] {op.faction} 구원군 {city.name} 도착 → 야전 교전")
        elif city.owner == op.faction:                # 아군 도시 선제 구원 = 주둔(합류). 적보다 먼저 와도 헛걸음 없음
            city.troops += op.committed_troops
            city.generals.extend(op.committed_generals)
            state.history.append(f"[작전{op.id}] {op.faction} 구원군 {city.name} 주둔(합류, 병력 {op.committed_troops})")
            state.operations.remove(op)
        else:                                         # 동맹 도시=합류 불가(병력 소유권) → 적 없으면 복귀
            _return_home(state, op, "출격 종료(대상 없음)")


def _join_friendly_siege(state: GameState, op: ActiveOperation, city) -> bool:
    """공성 target이 그 사이 아군·동맹이 된 경우 해소(⭐2026-08-31 아군 상잔 버그 수정).

    출발 시점 가드(start_operation)는 이동·교전 중의 소유 변경(아군의 선점 함락, 동맹 체결)을
    못 본다 → 도착·라운드 시점에 재검사. 아군=입성(합류, 별도 '입성' 지시 불필요) / 동맹=복귀
    (합류는 병력 소유권 위반 — 야전 구원 분기와 같은 원칙). 반환=해소됨.
    """
    if city.owner == op.faction:
        city.troops += op.committed_troops
        city.generals.extend(op.committed_generals)
        state.history.append(
            f"[작전{op.id}] {op.faction} {city.name} 입성(이미 아군 도시 — 공성 해소, 병력 {op.committed_troops})")
        state.operations.remove(op)
        return True
    if allied(state, op.faction, city.owner):
        _return_home(state, op, f"공성 취소({city.name}=동맹 도시)")
        return True
    return False


def _siege_round(state: GameState, op: ActiveOperation) -> None:
    """공성 교전 1라운드: 군대 vs 성 수비대(seam① 재사용, wall 보너스). 성벽 HP 0 = 돌파 → 함락.

    ⭐HP화(2026-09-01): 우세도가 공격군의 진행도 대신 **도시의 wall_hp를 깎는다** — 손상이 도시 소유라
    공성 주체가 바뀌어도(3파전) 물리적으로 정당하게 이어진다(구 "진행도 승계" 구멍 해소).
    공격=부대 사기(op.unit_morale), 수비=전역 사기(수성=작전 아님 → 세력값). 전멸/퇴각은 `_resolve_op_end`.
    """
    city = state.cities.get(op.action.target)
    if city is None:
        state.operations.remove(op)
        return
    if _join_friendly_siege(state, op, city):         # 교전 중 소유 변경(아군 상잔 방지) → 입성/복귀
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
    # 출성 감속(§9-20): 성 앞 아군 출성 부대 규모에 비례해 성벽 피해가 줄어든다(정지는 없음, 캡).
    sortie_troops = sum(o.committed_troops for o in state.operations
                        if o.faction == city.owner and o.stage == "교전"
                        and o.action.mode == "야전" and o.action.origin == o.action.target == op.action.target)
    slow = min(SORTIE_SLOW_CAP, sortie_troops / op.committed_troops) if sortie_troops and op.committed_troops > 0 else 0.0
    dmg = max(0, round(dominance * SIEGE_RATE * WALL_HP_SCALE * (1 - slow)))
    city.wall_hp = max(0, _wall_hp(city) - dmg)
    slow_note = f", 출성 견제 −{slow:.0%}" if slow else ""
    state.history.append(                             # 라운드 가시화(F층 관측성·관전): 무음 공성 해소
        f"[작전{op.id}] {op.faction} {city.name} 공성 중 "
        f"(성벽 {city.wall_hp}/{_wall_max(city)}, 병력 {op.committed_troops} vs 수비 {city.troops}{slow_note})")
    # 돌파(HP 0)여도 우세도 ≤0이면 함락 아님 — 이미 무너진 성도 수비대가 우세하면 지켜낸다(약체 공성의 날먹 방지).
    if op.committed_troops > 0 and (city.troops <= 0 or (city.wall_hp <= 0 and dominance > 0)):
        _capture_city(state, op, city)


def _capture_city(state: GameState, op: ActiveOperation, city) -> None:
    """함락: 소유 이전 + 약탈 + 방어 장수 탈출/포로 판정(포위도·seeded RNG) + 군주 자동 승계.

    포위도 = 1 − (패자 소유 인접 도시 / 총 인접). 공격 출발지가 아군 아니라 항상 >0.
    포획확률 = max(CAPTURE_FLOOR, 포위도²)(볼록: 제대로 에워싸야 급증). 완전 포위=탈출로0=확정.
    성벽 돌파 함락(잔존병 있음) 시 잔병×생존율이 인접 아군 도시로 퇴각 — 완전 포위면 전멸.
    장수는 탈출/포로 판정. 군주 포획=승계 트리거(멸망은 도시0). [[DISCUSSION#9-10]]·[[DISCUSSION#9-16]]
    """
    loser, winner = city.owner, op.faction
    defenders = list(city.generals)                 # 접수 전 방어 장수(군주 포함 가능)

    neighbors = list(state.distances.get(city.name, {}))
    total = len(neighbors) or 1
    escape_dests = sorted(n for n in neighbors
                          if n in state.cities and state.cities[n].owner == loser)
    encircle = 1 - len(escape_dests) / total
    capture_prob = max(CAPTURE_FLOOR, encircle ** 2)

    # 성벽 돌파 함락: 잔존 수비병은 증발 대신 퇴각(장수 탈출과 같은 포위 논리 — 퇴로 없으면 전멸)
    survivors = round(city.troops * SIEGE_RETREAT_SURVIVAL) if city.troops > 0 and escape_dests else 0

    # 도시 접수(금·식량=도시에 그대로=약탈, 병사=생존 공격군 주둔)
    city.owner = winner
    city.troops = op.committed_troops
    city.generals = list(op.committed_generals)
    state.history.append(f"[작전{op.id}] {winner}, {city.name} 함락 (구 {loser})")
    if survivors:
        state.cities[escape_dests[0]].troops += survivors
        state.history.append(f"  ↳ 수비 잔병 {survivors} {escape_dests[0]}(으)로 퇴각")
    _chronicle(state, f"{winner}, {loser}의 {city.name} 함락")
    _shift_morale(state, winner, MORALE_CITY_TAKEN, f"{city.name} 함락")
    _shift_morale(state, loser, MORALE_CITY_LOST, f"{city.name} 상실")
    if op in state.operations:
        state.operations.remove(op)

    for g in defenders:                              # 장수별 탈출/포로 (seeded RNG)
        if escape_dests and state.rng.random() >= capture_prob:
            state.cities[escape_dests[0]].generals.append(g)
            state.history.append(f"  ↳ {g} {escape_dests[0]}(으)로 탈출")
        else:
            state.history.append(f"  ↳ {g} 포로 (포위도 {encircle:.2f})")
            if _take_prisoner(state, city.name, g):   # 군주 포획: 승계는 처분 확정까지 보류(§9-21 정정)
                _chronicle(state, f"{winner}, {loser} 군주 {g} 포획")

    lf = state.factions.get(loser)
    if lf is not None and loser != "중립":
        remaining = [n for n, c in state.cities.items() if c.owner == loser]
        if not remaining:                            # 전 도시 상실 → 세력 소멸
            lf.alive = False
            state.alliances = [p for p in state.alliances if loser not in p]      # 망국의 동맹·제안 자동 소멸
            state.proposals = [p for p in state.proposals if loser not in (p.from_faction, p.to_faction)]
            # ⭐2026-09-01 좀비 세력 차단: 망국의 잔존 출전 부대는 정복 세력에 투항(마지막 함락 도시로 흡수).
            # 투항 장수만 승자 소속 전환 — 타지 수감 포로 등은 원 소속 유지(망국 인재 설득 개방 §9-21⑤ 보존).
            for op2 in [o for o in state.operations if o.faction == loser]:
                city.troops += max(0, op2.committed_troops)
                city.generals.extend(op2.committed_generals)
                city.prisoners.extend(op2.cargo_prisoners)
                city.gold += op2.cargo_gold
                city.food += op2.cargo_food
                for g in op2.committed_generals:
                    if g in state.generals:
                        state.generals[g].faction = winner
                        state.generals[g].is_ruler = False
                state.operations.remove(op2)
                state.history.append(f"[작전{op2.id}] {loser} 잔존 부대, {winner}에 투항(병력 {op2.committed_troops})")
            state.history.append(f"⚔ {loser} 멸망 (전 도시 상실)")
            _chronicle(state, f"{loser} 멸망")
    _liberate_prisoners(state, city)                 # 소유 변경된 감옥 정리(자국 포로 해방, ⭐2026-09-01)


def _liberate_prisoners(state: GameState, city: City) -> None:
    """소유 변경된 도시의 감옥 정리(⭐2026-09-01): 새 소유주 소속 포로 = 해방·즉시 합류.

    함락(탈환)·항복 흡수 공용 — "내 군주/장수가 내 도시의 포로로 영구 감금"되는 경계 구멍의 단일 수리 지점.
    타 세력 포로는 그대로 승계(약탈 관례). 해방자는 즉결 질의 큐에서도 제거.
    """
    freed = [p for p in list(city.prisoners)
             if p in state.generals and state.generals[p].faction == city.owner]
    for p in freed:
        city.prisoners.remove(p)
        city.generals.append(p)
        state.history.append(f"  ↳ 아군 포로 {p} 해방({city.name} 수복)")
    if freed:
        state.pending_captives = [(c, g) for c, g in state.pending_captives
                                  if not (c == city.name and g in freed)]


def _succeed_ruler(state: GameState, faction: str) -> None:
    """군주 처형 시 최고 통솔 생존 장수 자동 승계(충성도·내분 X = 스코프 크립 회피). [[DISCUSSION#9-16]]

    ⭐후보 = 세력 소속 전체(출전 중 포함, 수감·처형자 제외) — 도시 주둔만 집계하면 전 장수 출전 중일 때
    ruler=""가 영구 공석이 되는 구멍(2026-09-01 전수조사)."""
    jailed = {p for c in state.cities.values() for p in c.prisoners}
    cand = [g.name for g in state.generals.values()
            if g.faction == faction and g.name not in jailed]
    if not cand:                                     # 승계할 장수 없음(전원 수감/사망)
        state.factions[faction].ruler = ""
        return
    heir = max(cand, key=lambda g: state.generals[g].command if g in state.generals else 0)
    state.generals[heir].is_ruler = True
    state.factions[faction].ruler = heir
    state.history.append(f"  ↳ {faction} {heir} 군주 승계")
    _chronicle(state, f"{faction}, {heir} 군주 승계")


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
            if a.faction == b.faction or allied(state, a.faction, b.faction):
                continue                              # 동맹이면 안 싸움(§9-22)
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


def _field_round(state: GameState, a: ActiveOperation, b: ActiveOperation,
                 killers: dict[int, str]) -> None:
    """야전 1라운드(부대 vs 부대, wall=0, 순수 대칭). 협공은 '두 번 맞음'으로 자연 발생(보너스 없음).

    killers: 이 라운드에서 상대를 궤멸시킨 세력 기록(마지막 타격 귀속) — 전리품·포로가 승자에게 가게(⭐2026-09-01).
    """
    a_loss, b_loss, _ = _combat_round(
        state,
        Force(a.committed_troops, a.committed_generals, morale=a.unit_morale),
        Force(b.committed_troops, b.committed_generals, morale=b.unit_morale))
    a.committed_troops -= a_loss
    b.committed_troops -= b_loss
    a.unit_morale = max(0, a.unit_morale - UNIT_MORALE_COMBAT_DROP)
    b.unit_morale = max(0, b.unit_morale - UNIT_MORALE_COMBAT_DROP)
    a.has_fought = b.has_fought = True
    if a.committed_troops <= 0:
        killers[a.id] = b.faction
    if b.committed_troops <= 0:
        killers[b.id] = a.faction
    state.history.append(
        f"[야전] 작전{a.id}({a.faction}) ↔ 작전{b.id}({b.faction}): -{a_loss}/-{b_loss}")


def _resolve_combat(state: GameState) -> None:
    """한 턴 전투 해소: 야전(필드 쌍) → 공성(수비대) → 종료(전멸/퇴각/포로) → 야전 승자 복귀."""
    pairs = _field_engagements(state)
    n_opp: dict[int, int] = {}
    killers: dict[int, str] = {}                     # 궤멸 op → 마지막 타격 세력(전리품 귀속, ⭐2026-09-01)
    for a, b in pairs:
        _field_round(state, a, b, killers)
        n_opp[a.id] = n_opp.get(a.id, 0) + 1
        n_opp[b.id] = n_opp.get(b.id, 0) + 1
    for op in list(state.operations):                # 공성 라운드(필드 피해 반영된 병력으로 → 협공 자연 발생)
        if op.action.mode == "공성" and op.stage == "교전":
            _siege_round(state, op)
    for op in list(state.operations):                # 종료 판정
        _resolve_op_end(state, op, n_opp.get(op.id, 0), killers.get(op.id))
    engaged = {op.id for pair in pairs for op in pair}
    for op in list(state.operations):                # 야전 승자(교전 마치고 상대 소멸) → 자동 복귀(서브초이스2)
        if op.action.mode != "야전" or op.id in engaged or op.committed_troops <= 0:
            continue
        if op.has_fought:
            _return_home(state, op, "요격 완료")
        elif (op.action.origin == op.action.target   # 대기 중 출성: 위협이 사라지면 성내 복귀(합류)
              and not _city_threats(state, op.action.target, op.faction)):
            _return_home(state, op, "출성 해제(위협 소멸)")


def _resolve_op_end(state: GameState, op: ActiveOperation, n_field: int,
                    killer: str | None = None) -> None:
    """전멸(야전 피해→포로 / 공성 붕괴→격퇴) + 확률적 강제 퇴각(사기 붕괴)."""
    if op not in state.operations:                   # 이미 함락 등으로 해소
        return
    if op.committed_troops <= 0:
        if n_field > 0:
            _destroy_field_op(state, op, n_field, killer)  # 야전 전멸 → 장수 포획(base×부대수)·화물 노획
        elif op.action.mode != "호송":               # 장수 단독 호송(병력 0)은 정상 → 계속 간다
            # 격퇴 사기 훅은 안 둠(⭐사기=세력 정신적 무장 → 국지 전투 승패는 제외, 함락/상실/피랍만)
            _return_home(state, op, "격퇴" if op.action.mode == "공성" else "무위", troops=False)
        return
    # 야전(출성·구원 포함)은 실제 교전한 턴만 fighting — 성 앞 대기 중인 예비 출성이 퇴각 판정에 안 걸리게.
    fighting = (op.stage == "교전" and op.action.mode == "공성") or n_field > 0
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
    """장수 g를 holding_city 수감. 군주면 True 반환(연혁용).

    ⭐승계는 여기서 하지 않는다(§9-21 정정): 포획=군주 신분 유지(보류),
    처형 확정 때만 승계, 석방=군주 그대로 복귀. 설득 불가는 is_ruler 체크가 담당.
    """
    state.cities[holding_city].prisoners.append(g)
    state.pending_captives.append((holding_city, g))  # 즉결 처분 질의 대상(포획 시 1회)
    gen = state.generals.get(g)
    if gen is not None and gen.is_ruler:
        _shift_morale(state, gen.faction, MORALE_RULER_CAPTURED, f"군주 {g} 피랍")
        return True
    return False


def _destroy_field_op(state: GameState, op: ActiveOperation, n_field: int,
                      killer: str | None = None) -> None:
    """야전 전멸: 장수 포획확률 = base × 교전 적 부대 수. 실패=아군 복귀.

    ⭐포로·노획 = 전멸시킨 세력(killer, 마지막 타격) 귀속(2026-09-01) — 구 "지리적 최근접 적성 도시"는
    싸우지도 않은 제3세력·동맹이 수취하던 구멍. killer 불명(공성 중 소멸 등)일 때만 구 규칙 폴백.
    """
    prob = min(1.0, FIELD_CAPTURE_BASE * n_field)
    jail = None
    if killer:
        jail = _nearest_city_of(state, op, killer) or next(
            (n for n in sorted(state.cities) if state.cities[n].owner == killer), None)
    if jail is None:
        jail = _nearest_enemy_city(state, op)
    home = _home_city(state, op)
    for g in list(op.committed_generals):
        if jail and state.rng.random() < prob:
            if _take_prisoner(state, jail, g):        # 군주 포획: 승계는 처분 확정까지 보류(§9-21 정정)
                _chronicle(state, f"{op.faction} 군주 {g} 야전 포획")
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


# ======================= 담화 (포로 즉결 처분) [[DISCUSSION#9-21]] =======================
def _release_prisoner(state: GameState, city_name: str, prisoner: str) -> str | None:
    """포로를 원 세력 최근접 도시로 방면(수감 도시 인접 우선, 없으면 아무 자국 도시, 그것도 없으면 재야).

    석방·포로반환(몸값)이 공용. 반환값=도착 도시(재야면 None).
    """
    state.cities[city_name].prisoners.remove(prisoner)
    pf = state.generals[prisoner].faction if prisoner in state.generals else ""
    dest = next((n for n in state.distances.get(city_name, {})
                 if n in state.cities and state.cities[n].owner == pf),
                next((n for n in sorted(state.cities) if state.cities[n].owner == pf), None))
    if dest:
        state.cities[dest].generals.append(prisoner)
    return dest


def pending_dispositions(state: GameState) -> list[tuple[str, str]]:
    """처분 대기 포로 (도시, 포로) 목록. 턴 해소 직후 드라이버(턴 루프 호출자)가 순회하며 질의.

    엔진은 나열·판정만 — LLM 질의는 드라이버 몫(decide→engine 단방향 유지).
    """
    return [(c.name, p) for c in state.cities.values() for p in list(c.prisoners)]


def persuade_chance(state: GameState, city_name: str, prisoner: str, persuader: str) -> float:
    """설득 확률(LLM 경로, 담화 안 돌리는 쪽) = (BASE + 설득 장수 지력/SCALE) × (1 − 포로 충의/100).

    ⭐주체 = 처분에서 지정한 그 도시 주둔 장수(도시 최고지력 자동 아님, §9-21 정정).
    ⭐cap 폐지: 충의는 확률 천장이 아니라 감쇄 계수(플레이어 경로에선 페르소나·심판에 내재).
    현직 군주(is_ruler)·충의 100·주체 부재/타지는 0. 외교 협상도 이 베이스 재사용 예정.
    """
    city = state.cities.get(city_name)
    gen = state.generals.get(prisoner)
    agent = state.generals.get(persuader)
    if city is None or gen is None or agent is None or persuader not in city.generals:
        return 0.0
    base = PERSUADE_BASE + agent.intel / PERSUADE_INTEL_SCALE
    lf = state.factions.get(gen.faction)
    if lf is None or not lf.alive:                    # ⭐원 세력 멸망 = 지킬 주군이 없음 → 충의 감쇄 미적용, 군주도 설득 가능
        return base
    if gen.is_ruler:                                  # 현직 군주(세력 생존) = 설득 불가(세력 흡수 가드레일)
        return 0.0
    return base * (1 - gen.loyalty / 100)


def apply_disposition(state: GameState, city_name: str, prisoner: str, choice: str) -> bool:
    """포로 즉결 처분(석방/처형/수감) — 포로 상태 정리만. ⭐설득은 처분이 아니라 이후 턴의 명령(Persuade).

    처형=로스터 제거(군주면 이제서야 승계) / 석방=원 세력 복귀(군주면 군주로) / 수감=유지(설득·몸값 협상 대기).
    """
    city = state.cities.get(city_name)
    if city is None or prisoner not in city.prisoners:
        state.history.append(f"[환각] 처분 대상 아님: {city_name}의 {prisoner} → 기각")
        return False
    owner = city.owner
    if choice == "처형":
        city.prisoners.remove(prisoner)
        gen = state.generals.pop(prisoner, None)
        _chronicle(state, f"{owner}, {prisoner} 처형")
        if gen is not None and gen.is_ruler:          # ⭐군주는 처형 확정 시점에 승계(§9-21 정정)
            lf = state.factions.get(gen.faction)
            if lf is not None and lf.alive:
                _succeed_ruler(state, gen.faction)
        return True
    if choice == "수감":                              # 처분 유예: 계속 가둬둠(이후 설득 명령·몸값 협상 대상). 무료.
        gen = state.generals.get(prisoner)
        lf = state.factions.get(gen.faction) if gen else None
        if gen is not None and gen.is_ruler and lf is not None and lf.alive:
            # ⭐군주(세력 생존)=석방/처형 2지선다(§9-21 원안 복원, 2026-09-01) — 수감 시 피랍 영구화 방지
            state.history.append(f"[환각] 군주 {prisoner}는 수감 불가(석방/처형만) → 기각")
            return False
        state.history.append(f"[담화] {owner}, {prisoner} 수감 유지")
        return True
    if choice == "석방":
        dest = _release_prisoner(state, city_name, prisoner)
        _chronicle(state, f"{owner}, {prisoner} 석방" + (f" → {dest}" if dest else "(재야)"))
        return True
    state.history.append(f"[환각] 알 수 없는 처분 '{choice}' → 기각")
    return False


def attempt_persuade(state: GameState, city_name: str, prisoner: str, chance: float) -> bool:
    """설득 굴림(공용 판정). LLM 경로=apply_persuade가 상수식 확률로, 플레이어 경로=담화 심판 채점 확률로 호출."""
    city = state.cities[city_name]
    if chance > 0 and state.rng.random() < chance:
        city.prisoners.remove(prisoner)
        city.generals.append(prisoner)
        state.generals[prisoner].faction = city.owner
        state.generals[prisoner].is_ruler = False     # 망국 군주 등용 시 군주 신분 소멸(일개 장수로)
        _chronicle(state, f"{prisoner}, {city.owner}에 귀순 (설득)")
        return True
    state.history.append(f"[담화] {city.owner}, {prisoner} 설득 실패 (확률 {chance:.0%})")
    return False


def apply_persuade(state: GameState, action: Persuade, actor: str | None = None) -> None:
    """설득 명령(⭐행동 턴의 Action — 즉결 처분에서 분리, 명령 슬롯 자연 소모). 검증 3겹 후 굴림."""
    city = state.cities.get(action.city)
    if city is None or (actor is not None and city.owner != actor):
        state.history.append(f"[위반] {actor} 설득 월권/무효 도시 '{action.city}' → 기각")
        return
    if action.prisoner not in city.prisoners:
        state.history.append(f"[환각] {city.owner} 설득 대상 아님 '{action.prisoner}' → 기각")
        return
    p = persuade_chance(state, action.city, action.prisoner, action.persuader)
    if p <= 0:
        state.history.append(f"[환각] {action.prisoner} 설득 불가(군주/설득 장수 무효) → 기각")
        return
    attempt_persuade(state, action.city, action.prisoner, p)


# ======================= 외교 (동맹·포로반환·항복권유) [[DISCUSSION#9-22]] =======================
def _faction_power(state: GameState, faction: str) -> int:
    """항복 게이트용 국력 = 총병력(도시+출전 부대) + 도시수×가중치. LLM 관여 0."""
    troops = sum(c.troops for c in state.cities.values() if c.owner == faction)
    troops += sum(o.committed_troops for o in state.operations if o.faction == faction)
    n_cities = sum(1 for c in state.cities.values() if c.owner == faction)
    return troops + n_cities * SURRENDER_CITY_WEIGHT


def surrender_gate(state: GameState, me: str, target: str) -> bool:
    """항복권유 성립 게이트(⭐2026-09-01 교체): 상대 잔여 도시 ≤ SURRENDER_CITY_GATE + 제안측 국력 우위.

    구 "국력 3배"는 대세력 흡수 직후 차순위 세력에 바로 성립하는 민감도 문제. 수락 여부는 여전히 상대 군주 LLM.
    앱 UI(위젯 노출)와 엔진 검증이 이 한 곳을 공유한다.
    """
    n = sum(1 for c in state.cities.values() if c.owner == target)
    return n <= SURRENDER_CITY_GATE and _faction_power(state, me) > _faction_power(state, target)



def apply_diplomacy(state: GameState, action: Diplomacy, actor: str | None = None) -> None:
    """외교 명령 처리(결정론 검증). 파기=즉시 효력, 동맹·포로반환=제안 큐(상대 군주 판단은 드라이버).

    신뢰 수치 없음 — 체결/파기/거절이 연혁·기록에 남고, 다음 판단의 근거는 LLM이 읽는 그 기록뿐.
    """
    me, t = actor, action.target_faction
    if me is None:
        state.history.append("[기각] 외교 주체 불명(actor 없음)")
        return
    if t == me or t not in state.factions or not state.factions[t].alive:
        state.history.append(f"[환각] {me} 외교 대상 무효 '{t}' → 기각")
        return
    envoy = action.envoy
    if envoy and (envoy not in state.generals or state.generals[envoy].faction != me):
        state.history.append(f"[환각] {me} 사신 '{envoy}' 무효(타국/부재) → 제외")
        envoy = ""

    if action.proposal == "파기":
        pair = tuple(sorted((me, t)))
        if pair in state.alliances:
            state.alliances.remove(pair)
            _chronicle(state, f"{me}, {t}와의 동맹 파기")
        else:
            state.history.append(f"[환각] {me}, 동맹 아닌 {t}에 파기 선언 → 기각")
        return

    if action.proposal == "포로반환":
        gen = state.generals.get(action.prisoner)
        held = any(action.prisoner in c.prisoners and c.owner == t for c in state.cities.values())
        if gen is None or gen.faction != me or not held:
            state.history.append(
                f"[환각] {me} 반환 요청 무효('{action.prisoner}'는 {t} 수감 중인 아군 아님) → 기각")
            return
        pay_g = max((c.gold for c in state.cities.values() if c.owner == me), default=0)
        pay_f = max((c.food for c in state.cities.values() if c.owner == me), default=0)
        if action.offer_gold > pay_g or action.offer_food > pay_f:
            state.history.append(f"[기각] {me} 몸값(금{action.offer_gold}·식{action.offer_food}) 지불 여력 부족")
            return
        state.proposals.append(Proposal(
            from_faction=me, to_faction=t, proposal="포로반환", prisoner=action.prisoner,
            offer_gold=action.offer_gold, offer_food=action.offer_food,
            envoy=envoy, message=action.message))
        state.history.append(
            f"[외교] {me}, {t}에 {action.prisoner} 반환 요청(몸값 금{action.offer_gold}·식{action.offer_food})")
        return

    if action.proposal == "항복권유":
        # ⭐게이트=결정론(성립 안 하면 확률 0이 아니라 제안 자체 불성립). 수락 여부만 상대 군주 판단.
        if not surrender_gate(state, me, t):
            state.history.append(f"[기각] {me}의 {t} 항복 권유 — 대세가 그만큼 기울지 않음(말기 세력에만 성립)")
            return
        if any(p.proposal == "항복권유" and p.to_faction == t for p in state.proposals):
            state.history.append(f"[기각] {t} 항복 권유 중복")
            return
        state.proposals.append(Proposal(from_faction=me, to_faction=t, proposal="항복권유",
                                        envoy=envoy, message=action.message))
        state.history.append(f"[외교] {me}, {t}에 항복 권유")
        return

    # 동맹 제안
    if allied(state, me, t):
        state.history.append(f"[환각] {me}-{t} 이미 동맹인데 재제안 → 기각")
        return
    if any(p.proposal == "동맹" and {p.from_faction, p.to_faction} == {me, t} for p in state.proposals):
        state.history.append(f"[기각] {me}-{t} 동맹 제안 중복")
        return
    state.proposals.append(Proposal(from_faction=me, to_faction=t, proposal="동맹",
                                    envoy=envoy, message=action.message))
    state.history.append(f"[외교] {me}, {t}에 동맹 제안" + (f" (사신 {envoy})" if envoy else ""))


def respond_proposal(state: GameState, prop: Proposal, accept: bool, reason: str = "") -> bool:
    """상대 군주의 판단(수락/거절)을 상태에 적용(결정론). 반환=성사 여부.

    포로반환 수락 시 재검증: 포로 아직 수감 + 지불 여력(그 사이 처형·소진됐을 수 있음).
    """
    if prop in state.proposals:
        state.proposals.remove(prop)
    a, b = prop.from_faction, prop.to_faction
    if not accept:
        state.history.append(f"[외교] {b}, {a}의 {prop.proposal} 제안 거절"
                             + (f" ({reason})" if reason else ""))
        return False
    if prop.proposal == "동맹":
        pair = tuple(sorted((a, b)))
        if pair not in state.alliances:
            state.alliances.append(pair)
            _chronicle(state, f"{pair[0]}-{pair[1]} 동맹 체결")
        return True
    if prop.proposal == "항복권유":                   # 수락 = b가 a에 항복(전 도시·군대·장수 헌납)
        lf = state.factions.get(b)
        if lf is None or not lf.alive:
            state.history.append(f"[외교] {b} 항복 무산(세력 소멸)")
            return False
        for op2 in [o for o in state.operations if o.faction == b]:   # 출전 부대 해산 → 가까운 도시로 귀속
            c = state.cities.get(op2.action.origin) or state.cities.get(op2.action.target)
            if c is not None:
                c.troops += max(0, op2.committed_troops)
                c.generals.extend(op2.committed_generals)
                c.prisoners.extend(op2.cargo_prisoners)
                c.gold += op2.cargo_gold
                c.food += op2.cargo_food
            state.operations.remove(op2)
        taken = [c for c in state.cities.values() if c.owner == b]
        for c in taken:
            c.owner = a
        for g in state.generals.values():
            if g.faction == b:
                g.faction = a
                g.is_ruler = False                    # 항복 군주 = 승자의 신하(일개 장수)
        for c in taken:                               # 흡수한 감옥 정리: a 소속(구 b 포함) 포로 해방(⭐2026-09-01)
            _liberate_prisoners(state, c)
        lf.alive = False
        state.alliances = [p for p in state.alliances if b not in p]
        state.proposals = [p for p in state.proposals if b not in (p.from_faction, p.to_faction)]
        _chronicle(state, f"{b}, {a}에 항복 (전 도시 헌납)")
        check_victory(state)
        return True
    # 포로반환
    jail = next((c for c in state.cities.values()
                 if c.owner == b and prop.prisoner in c.prisoners), None)
    payer_g = max((c for c in state.cities.values() if c.owner == a), key=lambda c: c.gold, default=None)
    payer_f = max((c for c in state.cities.values() if c.owner == a), key=lambda c: c.food, default=None)
    if (jail is None or payer_g is None or payer_g.gold < prop.offer_gold
            or payer_f is None or payer_f.food < prop.offer_food):
        state.history.append(f"[외교] {prop.prisoner} 반환 무산(포로 부재 또는 몸값 여력 소진)")
        return False
    payer_g.gold -= prop.offer_gold                   # 몸값: 자국 최대 보유 도시에서 지불 → 수감 도시가 수취
    payer_f.food -= prop.offer_food
    jail.gold += prop.offer_gold
    jail.food += prop.offer_food
    dest = _release_prisoner(state, jail.name, prop.prisoner)
    _chronicle(state, f"{b}, 몸값(금{prop.offer_gold}·식{prop.offer_food}) 받고 {prop.prisoner}를 {a}에 반환"
               + (f" → {dest}" if dest else ""))
    return True


# ======================= 경제 (매턴 수입·병량 소모) =======================
def _economy_tick(state: GameState) -> None:
    """턴 말 경제: 규모 비례 수입 → 주둔 병량 소모. 군량 부족=못 먹인 병사 탈영(결정론, ⭐2026-08-30).

    턴 말인 이유: 턴 초에 넣으면 LLM이 브리핑에서 본 숫자와 어긋나 검산 불일치·과투입 노이즈.
    size 0(테스트용 추상 도시)=경제 없음. 정상 틱은 로그 안 남김(16도시×매턴=스팸), 탈영만 기록.
    """
    # ponytail: 출전 부대는 병량 무소모(현지 조달 간주) — 장기 원정 어뷰징 보이면 출발지 청구로 승격.
    for c in state.cities.values():
        if c.size <= 0 or c.owner == "중립":
            continue
        c.gold += c.size * CITY_INCOME_GOLD
        c.food += c.size * CITY_INCOME_FOOD
        upkeep = c.troops // TROOPS_PER_FOOD
        if c.food >= upkeep:
            c.food -= upkeep
        else:                                         # 부족분만큼 탈영 → 유지 가능한 규모로 자기 교정
            desert = min(c.troops, (upkeep - c.food) * TROOPS_PER_FOOD)
            c.food = 0
            c.troops -= desert
            state.history.append(f"[병량] {c.name}({c.owner}) 군량 부족 — 병사 {desert} 탈영")


# ======================= 승리 판정 =======================
def check_victory(state: GameState) -> None:
    """승리 = 천하통일(전 도시 단일 세력) 단일 조건. 참수 즉시승리 폐기(§9-16). 세력 소멸=도시0."""
    owners = {c.owner for c in state.cities.values()}
    non_neutral = owners - {"중립"}
    if "중립" not in owners and len(non_neutral) == 1:
        state.winner = next(iter(non_neutral))
        state.history.append(f"★ {state.winner} 천하통일")
        _chronicle(state, f"{state.winner} 천하통일")


# ======================= 턴 오케스트레이션 =======================
def _dispatch(state: GameState, action, actor: str | None = None) -> None:
    if isinstance(action, Domestic):
        apply_domestic(state, action, actor)
    elif isinstance(action, Battle):
        start_operation(state, action, actor)        # 공성·야전 모두 진군 작전으로 개시
    elif isinstance(action, Transfer):
        start_transfer(state, action, actor)
    elif isinstance(action, OpCommand):
        apply_op_command(state, action, actor)
    elif isinstance(action, Diplomacy):
        apply_diplomacy(state, action, actor)
    elif isinstance(action, Persuade):
        apply_persuade(state, action, actor)
    elif isinstance(action, Scheme):
        state.history.append(f"[증분2] 계략({action.scheme_type}) 미구현 → 무시")


def _order_phase(action) -> int:
    """명령 카테고리(⭐2026-09-01 phase 확정): 0=외교 → 1=전투(출격·작전지시·호송) → 2=내정(내정·설득·계략)."""
    if isinstance(action, Diplomacy):
        return 0
    if isinstance(action, (Domestic, Persuade, Scheme)):
        return 2
    return 1


def advance_turn(state: GameState, actions: list | dict) -> None:
    """한 달 진행: 개시/내정 → 이동(마일스톤) → 전투 해소(야전·공성·퇴각·포로) → 승리 → 시간.

    actions: `{세력: Action | list[Action]}` dict면 행위자 소유권 검증 + 명령 상한(LLM 경로),
             그냥 list면 검증 생략·종전 순서 그대로(스크립트 데모·테스트가 상태를 직접 짜는 경우).

    ⭐dict 경로 처리 순서(2026-09-01 확정): **외교 → 전투 → 내정** 카테고리 phase.
    같은 세력·같은 카테고리 안은 적힌 순서 유지(회군→재출격 같은 턴 성립). 세력 간 순서=매턴
    시드 셔플(선공 편향 제거·재현 가능, 전 phase 공통 — UI·프롬프트에 고지). 모병→같은 턴 출격
    콤보는 의도적 소멸(내정이 마지막 — 신병은 다음 달부터). 파기→공격 배신 콤보=생존(외교 선행).
    """
    if isinstance(actions, dict):
        per_faction: dict[str, list] = {}
        for actor, acts in actions.items():
            acts = list(acts) if isinstance(acts, list) else [acts]
            if len(acts) > MAX_ORDERS_PER_TURN:
                state.history.append(
                    f"[환각] {actor} 명령 {len(acts)}건 > 상한 {MAX_ORDERS_PER_TURN} → 초과분 무시")
                acts = acts[:MAX_ORDERS_PER_TURN]
            per_faction[actor] = acts
        order = list(per_faction)
        state.rng.shuffle(order)
        items = [(actor, a) for phase in (0, 1, 2)
                 for actor in order
                 for a in per_faction[actor] if _order_phase(a) == phase]
    else:
        items = [(None, a) for a in actions]
    owners0 = {n: c.owner for n, c in state.cities.items()}   # 점령 회복 판정용 스냅샷(⭐사용자)
    for actor, a in items:
        _dispatch(state, a, actor)
    _advance_movement(state)                          # 이동중 작전 진행(필드 교전=고정)
    _resolve_combat(state)                            # 야전 쌍 → 공성 → 종료 판정
    _economy_tick(state)                              # 수입·병량(턴 말 — 다음 brief가 갱신값을 봄)
    captured = frozenset(n for n, c in state.cities.items() if c.owner != owners0.get(n))
    _wall_regen(state, skip=captured)                 # 평시 성벽 자연회복(피침·점령 턴 제외, ⭐HP화)
    for n in captured:                                # ⭐점령 응급 보수: 턴 말, 최대 HP의 RATIO만큼(전투 간섭 0)
        c = state.cities[n]
        m = _wall_max(c)
        healed = min(m, _wall_hp(c) + round(m * WALL_CAPTURE_HEAL_RATIO))
        if healed != _wall_hp(c):
            c.wall_hp = healed
            state.history.append(f"[점령] {n} 성벽 응급 보수 → {healed}/{m}")
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
