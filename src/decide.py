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

from typing import Literal

from pydantic import BaseModel, Field

from .config import SIEGE_BASE, STRATEGY_MAX_CHARS, WALL_HP_SCALE
from .llm import LLMError, structured_complete
from .models import Action, Domestic, FactionName, GameState
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
        parts += [f"식{c.food}", f"금{c.gold}"]
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
             + (f", 동맹={','.join(allies)}" if allies else ""),
             "[우리 도시]", *[_city_line(state, n, True) for n in mine],
             "[타 세력 도시]", *[_city_line(state, n, False) for n in other]]
    if state.chronicle:                              # 주요 연혁 전량(굵직한 것만이라 짧음) — 원한·대세 기억용
        lines += ["[주요 연혁]", *[f"- {c}" for c in state.chronicle]]
    if state.operations:
        lines.append("[진행 중 작전]")
        # 동행 장수 표기: 출전 장수는 도시 줄에서 빠지므로 여기 안 적으면 LLM 눈에 증발 → 미보유 재지정 환각
        lines += [f"- [{o.id}] {o.faction} {o.action.origin}→{o.action.target} {o.action.mode}"
                  + (f" 교전 중" if o.stage == "교전"     # 공성 게이지=도시 줄의 성벽 파손 표기(HP화)
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


def decide(state: GameState, faction: FactionName) -> list[Action] | None:
    """한 세력의 이번 달 명령 목록. 도시가 없으면 None(행동할 주체가 없음)."""
    fb = _fallback(state, faction)
    if fb is None:
        return None
    return structured_complete(
        Decision, SYSTEM.format(faction=faction), brief(state, faction), fallback=fb
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
        advance_turn(state, actions, judge=action_judge)   # ⭐전략·모병 심판 배선(스모크=실 채점)
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
