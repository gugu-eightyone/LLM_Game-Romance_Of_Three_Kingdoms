"""LLM_Game v2 — 판단층: 정세 → LLM → Action(의도).

하이브리드 경계(CLAUDE.md·[[DISCUSSION#8]]): 여기서 나오는 건 **의도**뿐이다.
검증·클램프·산수·확률은 전부 engine.py가 한다. 이 파일은 engine을 부르지 않는다(단방향).

- 상태 직렬화 `brief()` = 난이도 "보통"(전 정보 공개). 안개(밀정·지력)는 나중에 여기만 고치면 됨.
- 응답 스키마: `Action`은 Union이라 response_format에 직접 못 넣는다(루트 anyOf 거부).
  → `Decision(action: Action)` 래퍼로 property 자리에 놓으면 통과.
- 실패해도 앱은 안 죽는다: 폴백 = 금 0짜리 내정(상태 무변).
- 비용: 세력당 1호출/턴 = 3호출/턴. 턴 수 상한은 호출자 몫(데모가 상수로 잡음).
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, create_model

from .config import FOOD_ALERT_MONTHS, SIEGE_BASE, STRATEGY_MAX_CHARS, WALL_HP_SCALE
from .llm import LLMError, structured_complete
from .models import (Action, Battle, Diplomacy, Dispose, Domestic, FactionName,
                     GameState, OpCommand, Persuade, Scheme, Transfer, Travel)
from .prompts import load as load_prompt


class Decision(BaseModel):
    """구조화출력 래퍼. Union을 property로 내리기 위한 껍데기 한 겹 + 멀티 명령 목록."""
    actions: list[Action]                    # 1~4건. 상한 강제는 엔진(초과=[환각] 로깅, A층 표면)


# 프롬프트 원문은 prompts/*.txt (2026-08-30 이사 — "3개 이상" 트리거 충족. 버전=git 해시)
SYSTEM = load_prompt("decide_system")


# ======================= 특화 심판 (⭐2026-09-02 배선 — §9-11 kind 라우팅) =======================
class JudgeScore(BaseModel):
    """전략/방침 채점(1~10, 중립=5 — ⭐10점 통일). 확률·보정 환산은 엔진(_judge_mod) — 심판은 점수만(캡·산수=코드)."""
    score: int = Field(ge=1, le=10)
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


JUDGE_SYSTEMS = {"전투": load_prompt("strategy_judge"), "내정": load_prompt("domestic_judge")}


class JudgeScores(BaseModel):
    """일괄 채점(⭐2026-09-05 계획 단위 채점): scores는 입력 나열과 같은 순서."""
    scores: list[JudgeScore]


def _judged_texts(actions: dict) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(세력, 심판종류) → [(전략문, 행동 한줄)]. 엔진이 채점하는 집합과 동일(전투=출격·전략변경 / 내정)."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for faction, acts in actions.items():
        for a in (acts if isinstance(acts, list) else [acts]):
            text = getattr(a, "strategy", "")
            if not text:
                continue
            if a.kind == "전투":
                ctx = f"{a.mode} {a.origin}→{a.target} 병{a.troops}"
            elif a.kind == "작전지시" and a.order == "전략변경":
                ctx = f"작전{a.op_id} 전략변경"
            elif a.kind == "내정":
                ctx = f"{a.item} {a.city}"
            else:
                continue                              # 계략·설득 등은 각자 서브시스템(여기 채점 아님)
            out.setdefault((faction, "내정" if a.kind == "내정" else "전투"), []).append((text, ctx))
    return out


def turn_judge(state: GameState, actions: dict):
    """⭐계획 단위 일괄 채점(2026-09-05, 마찰 22): (세력,종류)별 이번 턴 전략 전부를 한 호출로 채점.

    분할 협공이 낱개 병력으로 "과장" 삼중 감점되던 문맥 결손 해소 + 호출 N→1. 엔진 JudgeFn과
    호환되는 콜백을 돌려줌(엔진 무변). 채점 상태=턴 시작 brief(행위자 정보 집합과 동일 — 상대
    같은 턴 숨은 수는 의도적으로 안 봄, 카운터는 상성 판정·전략변경 재채점 몫). 단건·캐시 미스·
    실패·길이 불일치는 기존 단건 action_judge로 폴백(악화 없음).
    """
    cache: dict[tuple[str, str, str], tuple[int, str]] = {}
    for (faction, kind), items in _judged_texts(actions).items():
        if len(items) < 2:                            # 단건=기존 경로와 동일 거동
            continue
        listing = "\n".join(f"{i}. ({ctx}) 「{t}」" for i, (t, ctx) in enumerate(items, 1))
        try:
            v = structured_complete(
                JudgeScores, JUDGE_SYSTEMS[kind].format(faction=faction),
                brief(state, faction)
                + f"\n\n[채점 대상 — {faction}의 이번 턴 {kind} 전략 {len(items)}건. "
                f"같은 턴에 함께 내려진 한 계획의 부분들이다 — 각각을 계획 전체 맥락에서 채점, "
                f"scores 배열은 번호 순서대로]\n{listing}")
        except LLMError:
            continue
        if len(v.scores) != len(items):
            continue                                  # 배열 길이 불일치 → 단건 폴백(안전)
        for (t, _), sc in zip(items, v.scores):
            cache[(faction, kind, t)] = (sc.score, sc.reason)

    def judge(st: GameState, faction: str, kind: str, text: str) -> tuple[int, str] | None:
        return cache.get((faction, kind, text)) or action_judge(st, faction, kind, text)
    return judge


class MatchupVerdict(BaseModel):
    """상성 판정: 명확히 맞물릴 때만 갑/을 — 애매하면 없음(기본값이 보수 쪽)."""
    advantage: Literal["갑", "을", "없음"] = "없음"
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


MATCHUP_SYSTEM = load_prompt("matchup_judge")


def matchup_judge(state: GameState, ctx: str, fa: str, ta: str,
                  fb: str, tb: str) -> tuple[int, str] | None:
    """⭐교전 상성 소심판(2026-09-05): 실제 맞붙은 두 전략의 상호작용만 판정(품질 재채점 금지 — 이중 계상 방지).

    입력=두 전략문+교전 한 줄(전황 전체 안 실음 — 데이터 최소). 실패=None(보정 0, 공짜 없음).
    """
    try:
        v = structured_complete(
            MatchupVerdict, MATCHUP_SYSTEM,
            f"[교전] {ctx}\n갑({fa}): 「{ta}」\n을({fb}): 「{tb}」")
        return {"갑": 1, "을": -1}.get(v.advantage, 0), v.reason
    except LLMError:
        return None


def action_judge(state: GameState, faction: str, kind: str, text: str) -> tuple[int, str] | None:
    """엔진에 주입하는 심판 콜백(§9-11: kind로 코드 라우팅 → 도메인 특화 심판, 분류 LLM 없음).

    채점 근거=정세 브리핑(상태 정합성 채점 → 미사여구 gaming 방어, §9-12). 실패=None(보정 0, 공짜 없음).
    """
    system = JUDGE_SYSTEMS.get(kind)
    if system is None:
        return None
    try:
        v = structured_complete(
            JudgeScore, system.format(faction=faction),
            brief(state, faction) + f"\n\n[채점 대상 {kind} 전략] {text}")
        return v.score, v.reason
    except LLMError:
        return None


def _city_line(state: GameState, name: str, own: bool) -> str:
    c = state.cities[name]
    # 병0 도시는 "출병 불가"를 글자로 박음(잔여 병력 대조를 mini에게 추론시키지 않기)
    wall_max = (SIEGE_BASE + c.wall) * WALL_HP_SCALE  # 파손 성벽만 표기(온전=무표기, 토큰 절약). engine 미임포트(단방향)
    wall = f"벽{c.wall}" + (f"(파손 {c.wall_hp}/{wall_max})" if 0 <= c.wall_hp < wall_max else "")
    parts = [f"병{c.troops}" + ("(출병 불가)" if own and c.troops <= 0 else ""), wall]
    friends = {x for pair in state.alliances if c.owner in pair for x in pair}  # 동맹은 위협 아님(§9-22)
    if own:  # 피침 경보: "내 도시가 공격받는 중"을 추론시키지 않고 결론으로 박음(전부 결정론)
        for t in state.operations:
            if t.faction != c.owner and t.faction not in friends \
                    and getattr(t.action, "target", None) == name \
                    and t.action.mode in ("공성", "야전"):
                eta = ("성 앞 교전 중" if t.stage == "교전"
                       else f"{t.action.origin} 방면에서 약 {max(0.0, t.threshold - t.progress):g}개월 후 도착")
                parts.append(f"⚠피침: {t.faction}군 {t.committed_troops} {eta}")
    if own:
        from .engine import food_net, food_runway     # 드라이버와 같은 지연 임포트(단방향: engine은 decide를 모름)
        net = food_net(state, name)                   # ⭐월 수지 상시 박기: 소비 산수를 결론으로(역주행 관찰 대응)
        parts += [f"식{c.food}" + (f"(월{net:+d})" if net is not None else ""), f"금{c.gold}"]
        runway = food_runway(state, name)
        if runway is not None and runway <= FOOD_ALERT_MONTHS:  # ⭐군량 경보: 결정론 결론 박기(피침 경보와 동형)
            parts.append("⚠군량: " + ("이번 달 고갈 위험" if runway == 0
                                      else f"현 소모율로 {runway}개월 내 고갈"))
    if c.generals:
        parts.append("장수 " + ",".join(f"{g}(통{state.generals[g].command})"
                                       if g in state.generals else g for g in c.generals))
    if c.prisoners:
        parts.append("포로 " + ",".join(c.prisoners))
    line = f"- {c.name}({c.owner}) " + "·".join(parts)
    if own:  # 인접은 우리 도시에만 붙인다 — 출병 가능한 목적지가 곧 이것이라(토큰 절약)
        # 도시별 "쓸 수 있는 동사"를 명시 태깅: 소유 대조를 LLM이 추론하게 두지 않는다(자국 공성 환각 대책).
        # 아군 방면도 구원야전은 정당(엔진 허용)이라 호송만 적으면 그 길이 가려짐 → 동사 목록으로.
        adj = [f"{n}(아군 {d}개월·호송/구원야전)" if state.cities[n].owner == c.owner
               else f"{n}(동맹 {state.cities[n].owner} {d}개월·구원야전만)" if state.cities[n].owner in friends
               else f"{n}({state.cities[n].owner} {d}개월·공성/야전)"
               for n, d in state.distances.get(name, {}).items() if n in state.cities]
        line += " | 인접: " + ", ".join(adj)
    return line


def brief(state: GameState, faction: FactionName) -> str:
    """그 세력이 보는 정세 한 장. 난이도 보통=전 정보 공개(안개는 밀정 붙일 때)."""
    f = state.factions[faction]
    mine = [n for n, c in state.cities.items() if c.owner == faction]
    other = [n for n, c in state.cities.items() if c.owner != faction]
    captive = any(f.ruler in c.prisoners for c in state.cities.values())   # 군주 피랍(승계 보류 중, §9-21)
    def _ally_tag(x: str) -> str:                     # 기한제(⭐): 잔여 개월 표기, 엔트리 없으면 무기한
        left = state.alliance_expires.get("|".join(sorted((faction, x))))
        return f"{x}({left}개월)" if left else x
    allies = [_ally_tag(x) for x in
              sorted({x for pair in state.alliances if faction in pair for x in pair} - {faction})]
    lines = [f"[{state.year}년 {state.month}월] 우리={faction}, 군주={f.ruler}"
             + ("(적에게 피랍!)" if captive else "") + f", 사기={f.morale}"
             # ⭐부정 케이스도 결론 박기(2026-09-02): 태그 부재로 "동맹 아님"을 추론시키면 파기 선언 환각
             + (f", 동맹={','.join(allies)}" if allies else ", 동맹=없음(파기 불가)"),
             "[우리 도시]", *[_city_line(state, n, True) for n in mine],
             "[타 세력 도시]", *[_city_line(state, n, False) for n in other]]
    # ⭐아군 피랍 결론 박기(2026-09-05): 타 세력 감옥의 우리 장수 — 소유 대조·동사 연결을 추론시키지 않는다
    # (자국 공성·파기 환각과 같은 수리 패턴. 피랍 없으면 줄 자체가 없음=토큰 0)
    captives = [(p, n) for n, c in state.cities.items() if c.owner != faction
                for p in c.prisoners if state.generals.get(p) and state.generals[p].faction == faction]
    if captives:
        lines.append("[아군 피랍] "
                     + ", ".join(f"{p}({state.cities[n].owner} {n} 수감)" for p, n in captives)
                     + " — 외교 '포로반환'(몸값 제시)으로 송환 요청 가능")
    if state.chronicle:                              # 주요 연혁 전량(굵직한 것만이라 짧음) — 원한·대세 기억용
        lines += ["[주요 연혁]", *[f"- {c}" for c in state.chronicle]]
    if state.operations:
        lines.append("[진행 중 작전]")
        # 동행 장수 표기: 출전 장수는 도시 줄에서 빠지므로 여기 안 적으면 LLM 눈에 증발 → 미보유 재지정 환각
        lines += [f"- [{o.id}] {o.faction} {o.action.origin}→{o.action.target} {o.action.mode}"
                  + (f" 교전 중" if o.stage == "교전"     # 공성 게이지=도시 줄의 성벽 파손 표기(HP화)
                     # ⭐길목 대기(D묶음): 지점 사수 중임을 결론으로 박음(재출격·회군 판단 재료)
                     else f" {o.action.origin}–{o.action.target} {o.action.hold_at}개월 지점 대기 중"
                     if getattr(o.action, "hold_at", 0) and o.progress >= o.action.hold_at
                     else f" 이동 {o.progress:g}/{o.threshold:g}")
                  + f" 병력{o.committed_troops} 사기{o.unit_morale}"
                  + (f" 전략보정{o.strategy_mod:+.0%}" if o.strategy_mod else "")
                  + (f" 장수 {','.join(o.committed_generals)}(출전 중)" if o.committed_generals else "")
                  + (" ← 아군: 자동 계속. 이 작전에 새 출격을 또 내리지 마라(별도 부대가 추가로 나간다) — 변경·철수는 작전지시로"
                     if o.faction == faction else "")
                  for o in state.operations]
    if state.history:
        lines += ["[최근 전황]", *[f"- {h}" for h in state.history[-8:]]]
    return "\n".join(lines)


def _fallback(state: GameState, faction: FactionName) -> Decision | None:
    """호출이 다 실패했을 때 반환할 무해한 명령 — 금 0 지출 내정(상태 무변, 로그만)."""
    mine = [n for n, c in state.cities.items() if c.owner == faction]
    if not mine:
        return None
    return Decision(actions=[Domestic(kind="내정", city=mine[0], item="식량증산", gold_spent=0)])


def _decision_model(state: GameState, faction: FactionName) -> type[Decision]:
    """⭐동적 스키마(2026-09-02): 장수 필드 2곳(출격 동행·내정 담당)을 도시-장수 쌍 variant로 제약.

    "그 도시에 있는 장수만"은 관계 제약이라 단일 enum으론 못 막는다 → 자국 도시별로
    (origin/city=Literal[도시], 장수=Literal[그 도시 주둔진]) variant를 만들어 anyOf로 묶는다.
    티칭 2회 실패(스모크 1.17/턴 → 강화 후 장기런 1.56/턴) 후 승격한 최후 카드 —
    측정된 실패 지점 2곳만. 나머지 동사는 정적 스키마+엔진 가드 유지(A층 측정 표면 보존).
    엔진 가드도 그대로 산다(플레이어·list 경로, 스키마가 놓치는 턴중 이동은 어차피 동시판정이라 없음).
    """
    battles: list[type[BaseModel]] = []
    doms: list[type[BaseModel]] = []
    for i, (name, c) in enumerate(state.cities.items()):
        if c.owner != faction:
            continue
        b_fields: dict = {"origin": (Literal[name], ...)}
        if c.generals:                               # 장수 없는 도시 = 제약할 목록이 없음(가드가 수비)
            b_fields["generals"] = (list[Literal[tuple(c.generals)]], Field(default_factory=list))
        battles.append(create_model(f"Battle{i}", __base__=Battle, **b_fields))
        doms.append(create_model(
            f"Domestic{i}", __base__=Domestic,
            city=(Literal[name], ...),
            general=(Literal[tuple(c.generals) + ("",)], ""),   # ""=미지정(배수 1.0)
        ))
    union = Union[tuple(battles + doms) + (Scheme, Transfer, Travel, OpCommand, Diplomacy, Persuade, Dispose)]
    return create_model("DynDecision", __base__=Decision, actions=(list[union], ...))


def decide(state: GameState, faction: FactionName) -> list[Action] | None:
    """한 세력의 이번 달 명령 목록. 도시가 없으면 None(행동할 주체가 없음)."""
    fb = _fallback(state, faction)
    if fb is None:
        return None
    return structured_complete(
        _decision_model(state, faction), SYSTEM.format(faction=faction),
        brief(state, faction), fallback=fb,
    ).actions


# ======================= 담화: 포로 즉결 처분 질의 [[DISCUSSION#9-21]] =======================
class Disposition(BaseModel):
    """포로 즉결 처분 응답(석방/처형/수감 — 포로 상태 정리만). ⭐설득은 이후 턴의 명령(kind=설득)."""
    choice: Literal["석방", "처형", "수감"]
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


DISPOSITION_SYSTEM = load_prompt("disposition")


def decide_disposition(state: GameState, city: str, prisoner: str) -> Disposition | None:
    """보유 세력 LLM에 포로 즉결 처분 질의(소호출). 실패 시 None=유예(수감 유지, 다음 턴 재질의)."""
    faction = state.cities[city].owner
    g = state.generals.get(prisoner)
    stat = f"통솔{g.command}·지력{g.intel}, 원 소속 {g.faction}" if g else "정보 없음"
    lines = [f"[포로] {prisoner} ({stat}) — {city}에 수감",
             f"[우리 사정] 도시 {sum(1 for c in state.cities.values() if c.owner == faction)}개, "
             f"사기 {state.factions[faction].morale}"]
    if state.chronicle:
        lines += ["[주요 연혁]", *[f"- {c}" for c in state.chronicle]]
    try:
        return structured_complete(
            Disposition, DISPOSITION_SYSTEM.format(faction=faction), "\n".join(lines))
    except LLMError:
        return None                                  # 보류: 상태 무변, 다음 턴 재질의


def resolve_dispositions(state: GameState, verbose: bool = False,
                         player: FactionName | None = None) -> None:
    """턴 해소 직후 드라이버 훅: **신규 포획자만** 보유 세력 LLM에 즉결 질의(석방/처형/수감) → 엔진 적용.

    수감 선택 후엔 재질의 없음 — 이후는 설득 명령(kind=설득)·몸값 협상의 영역(⭐사용자 재설계).
    player: 그 세력의 포획자는 LLM에 안 묻고 큐에 남김 → 턴 종료 결과 창이 처리(Q4 결정함수 교체와 동형).
    """
    from .engine import apply_disposition

    pending, state.pending_captives = list(state.pending_captives), []
    for city, prisoner in pending:
        c = state.cities.get(city)
        if c is None or prisoner not in c.prisoners:   # 그 사이 이동·소멸
            continue
        faction = c.owner
        if faction not in state.factions or not state.factions[faction].alive:
            continue
        if faction == player:                          # 플레이어 몫 → 결과 창이 소비하게 잔존
            state.pending_captives.append((city, prisoner))
            continue
        d = decide_disposition(state, city, prisoner)
        if d is None:                                  # 호출 실패 → 다음 턴 재질의
            state.pending_captives.append((city, prisoner))
            continue
        if verbose:
            print(f"  [담화] {faction} → {prisoner}: {d.choice} ({d.reason})")
        if not apply_disposition(state, city, prisoner, d.choice):
            state.pending_captives.append((city, prisoner))   # 무효 처분(군주 수감 등) → 다음 턴 재질의


# ======================= 외교: 제안 응답 질의 [[DISCUSSION#9-22]] =======================
class ProposalResponse(BaseModel):
    """외교 제안에 대한 상대 군주의 판단. 수락/거절은 이해득실 판단이라 확률(RNG) 없음 — 효과는 엔진."""
    accept: bool
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


PROPOSAL_SYSTEM = load_prompt("proposal_response")


def decide_proposal_response(state: GameState, prop) -> ProposalResponse | None:
    """제안받은 세력 군주 LLM에 수락/거절 질의(소호출). 실패 시 None=보류(제안 잔존, 다음 턴 재질의)."""
    detail = (f"[제안] {prop.from_faction}의 동맹 제안(기한 {prop.months or 12}개월"
              f" — 이미 동맹이면 연장 제안)" if prop.proposal == "동맹"
              else f"[제안] {prop.from_faction}의 항복 권유 — 수락하면 우리의 전부를 넘기고 나라를 접는다"
              if prop.proposal == "항복권유"
              else f"[제안] {prop.from_faction}의 포로 {prop.prisoner} 반환 요청"
                   f" — 몸값 금{prop.offer_gold}·식량{prop.offer_food}")
    if prop.envoy:
        detail += f" (사신 {prop.envoy})"
    if prop.message:
        detail += f"\n[국서] {prop.message}"
    try:
        return structured_complete(
            ProposalResponse, PROPOSAL_SYSTEM.format(faction=prop.to_faction, proposer=prop.from_faction),
            brief(state, prop.to_faction) + "\n\n" + detail)
    except LLMError:
        return None


def resolve_proposals(state: GameState, verbose: bool = False,
                      player: FactionName | None = None) -> None:
    """턴 해소 직후 드라이버 훅: 대기 외교 제안을 상대 군주 LLM에 질의 → 엔진 적용.

    player: 그 세력이 받는 제안(항복 권유 포함)은 LLM에 안 묻고 큐에 남김 → 결과 창에서
    직접 수락/거절(수락한 항복 권유 = 패배 엔딩. Q4 결정함수 교체와 동형, UI는 Streamlit 때).

    ⭐2026-09-01 재작성(전수조사 🔴2·5): 큐를 통째 비우고 한 통씩 소비(pop 모델) — 같은 턴 항복
    수락이 큐를 필터링해도 이중 삭제 크래시(ValueError)가 없다. 발신 세력이 그 사이 소멸한 제안도
    폐기(죽은 세력과 동맹 체결 방지). 보류·플레이어 몫만 큐에 되돌린다.
    """
    from .engine import respond_proposal

    pending, state.proposals = list(state.proposals), []
    for prop in pending:
        sender = state.factions.get(prop.from_faction)
        target = state.factions.get(prop.to_faction)
        if (target is None or not target.alive
                or sender is None or not sender.alive):   # 수신/발신 세력 소멸 → 제안 폐기
            continue
        if prop.to_faction == player:                 # 플레이어 몫 → 결과 창이 소비하게 잔존
            state.proposals.append(prop)
            continue
        r = decide_proposal_response(state, prop)
        if r is None:                                 # 호출 실패 → 보류(다음 턴 재질의)
            state.proposals.append(prop)
            continue
        if verbose:
            print(f"  [외교] {prop.to_faction} ← {prop.from_faction} {prop.proposal}: "
                  f"{'수락' if r.accept else '거절'} ({r.reason})")
        respond_proposal(state, prop, r.accept, r.reason)


def decide_all(state: GameState) -> dict[FactionName, list[Action]]:
    """살아있는 전 세력의 명령. `advance_turn(state, 이 dict)`에 그대로 넣으면 소유권·상한까지 검증됨."""
    out: dict[FactionName, list[Action]] = {}
    for name, f in state.factions.items():
        if not f.alive:
            continue
        a = decide(state, name)
        if a:
            out[name] = a
    return out


def demo(turns: int = 6) -> None:
    """실 API로 자율 시뮬 몇 턴. `python -m src.decide [턴수]` (비용 = 3호출 × 턴수)."""
    from .engine import advance_turn, load_scenario

    state = load_scenario()
    for t in range(turns):
        h0 = len(state.history)
        actions = decide_all(state)
        for f, acts in actions.items():
            for a in acts:
                print(f"  {f}: {a.model_dump_json(exclude_defaults=True)}")
        # ⭐계획 단위 일괄 채점 + 교전 상성(2026-09-05) — 스모크=실 채점
        advance_turn(state, actions, judge=turn_judge(state, actions), matchup=matchup_judge)
        resolve_dispositions(state, verbose=True)    # 포획 포로 즉결 처분(§9-21)
        resolve_proposals(state, verbose=True)       # 외교 제안 응답(§9-22)
        print(f"[{state.year}년 {state.month}월 종료]")
        print("\n".join(state.history[h0:]) or "  (변화 없음)")
        if state.winner:
            print(f"★ 승자: {state.winner}")
            break


if __name__ == "__main__":
    import sys
    demo(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
