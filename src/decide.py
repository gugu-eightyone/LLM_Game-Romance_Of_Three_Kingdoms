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

from .config import STRATEGY_MAX_CHARS
from .llm import LLMError, structured_complete
from .models import Action, Domestic, FactionName, GameState


class Decision(BaseModel):
    """구조화출력 래퍼. Union을 property로 내리기 위한 껍데기 한 겹 + 멀티 명령 목록."""
    actions: list[Action]                    # 1~4건. 상한 강제는 엔진(초과=[환각] 로깅, A층 표면)


SYSTEM = """당신은 삼국지 시대 {faction}의 군주다. 주어진 정세를 읽고 이번 달 명령 목록(1~4건)을 정하라.

규칙(어기면 그 명령은 기각된다):
- 명령은 적힌 순서대로 즉시 처리된다(예: 모병을 먼저 적으면 그 병력으로 같은 달 출격 가능).
- 전투는 반드시 **우리 도시**에서 출발하고, 목표는 그 도시의 **인접 목록에 있는 도시**여야 한다.
- 출격 명령의 origin_troops_seen에는 **브리핑에 적힌 출발 도시의 현재 보유 병력을 그대로 베껴 적어라**
  (투입량이 아니다). 투입 troops는 그 이하에서 자유.
- 진행 중인 부대에 병력을 직접 보탤 수는 없다. **증원 = 같은 목표로 추가 출격**(도착하면 합류 협공이 된다).
- 투입 병력은 출발 도시의 보유 병력 이내. 동행 장수는 그 도시에 주둔한 장수만.
- mode 공성 = **다른 세력의** 인접 도시를 점령한다. 목표는 인접 목록에 **「공성/야전」으로 표시된 도시만**(아군 도시 공성=기각).
- mode 야전 = 도로로 출격해 적 부대와 싸운다. 목표가 **적 도시면 그 방면 요격**, **아군 도시면 구원 출격** —
  적이 있으면 싸우고, 적이 아직 없으면 **그 도시에 주둔(합류)해 수비를 강화**한다(=증원). 거리 2개월 이상인 도로에서만 마주친다.
- **⚠피침 표시가 붙은 우리 도시**는 공격받는 중이다. 대응 4가지: ① 접근 중인 적을 길에서 요격 = **그 도시에서
  적이 오는 방면(경보에 표시된 방면 도시)으로 야전 출격** ② 다른 도시에서 그 도시로 구원 야전(주둔) ③ 호송으로 증원
  ④ 그 도시를 출발지이자 목표로 하는 야전 = **출성**(수비대 일부가 성 앞에 진 친다. 적이 도착하면 성벽 밖에서
  먼저 싸워 공성 진행을 늦추고, 적이 물러가면 자동으로 성에 복귀한다. 출성 부대는 성벽 보너스가 없고,
  병력을 너무 빼면 남은 수성이 약해진다). 공성으로는 지원할 수 없다.
- **수성은 자동이다** — 주둔 병력은 명령 없이도 성벽 보너스를 받으며 그 도시를 지킨다(단 성벽은 남은 병력이
  지키는 만큼만 위력을 낸다). 제자리 야전(출발지=목표)은 **⚠피침 도시에서만 출성**으로 성립하고 그 외엔
  기각된다.
- 호송(kind=호송) = 병사·장수·포로·금·식량을 **인접한 우리 도시**로 보낸다. 병사·물자·포로를 실으면
  호위 병사 200 이상 동원. 장수만 보낼 땐 병사 0도 된다.
- 내정은 우리 도시에 금을 투입한다. 식량증산·모병(금 1 → 2), 성벽보수, 사기진작.
- 작전지시(kind=작전지시) = **진행 중인 우리 작전**([진행 중 작전]의 번호로 지정)에 지시한다.
  order 전략변경 = 새 strategy를 내려 전술을 갱신한다(전황이 바뀌면 적극 활용하라).
  order 회군 = 부대를 철수시킨다(교전 중 회군은 퇴각 손실). 회군한 병력은 같은 달 새 출격에 쓸 수 있다.
- 이미 진행 중인 작전은 저절로 계속된다. 같은 작전을 다시 출격 명령하지 마라(병력만 낭비된다).
  출발 도시에 남은 병력이 있을 때만 추가 출병이 가능하다(병0 = 출병 불가).
- 설득(kind=설득) = **우리 도시에 수감된 포로**의 등용을 시도한다. persuader에 그 도시 주둔 우리 장수를
  지정하라(그 장수의 지력이 성공 확률을 정한다. 군주 포로는 설득 불가). 실패해도 포로는 남는다(재시도 가능).
- 외교(kind=외교) = proposal 동맹: 다른 세력에 동맹을 제안한다(message 한 줄 국서, envoy에 우리 장수를
  사신으로 지정 가능). 상대 군주가 수락해야 성립한다. proposal 파기: **동맹 중일 때만**(브리핑에
  「동맹=」 표시가 있을 때) 그 동맹을 깬다(즉시 효력 — 같은 달 이어지는 공격도 가능하나 배신은 기록에
  남는다). proposal 포로반환: 적에게 잡힌 **우리 장수**를
  몸값(offer_gold·offer_food)을 제시해 되사온다(prisoner에 장수 이름). 상대가 수락해야 성립.
- 동맹 중인 세력의 도시는 공성할 수 없다(치려면 먼저 파기). 「동맹」 표시 도시가 공격받으면
  야전(구원)으로 도울 수 있다.
- 계략(kind=계략)은 아직 미구현이니 절대 고르지 마라.
- strategy는 50자 이내 한국어 한 줄.

승리 조건은 천하통일이다. 다만 무리한 원정보다 전선 유지·국력 축적이 나은 달도 있다."""


def _city_line(state: GameState, name: str, own: bool) -> str:
    c = state.cities[name]
    # 병0 도시는 "출병 불가"를 글자로 박음(잔여 병력 대조를 mini에게 추론시키지 않기)
    parts = [f"병{c.troops}" + ("(출병 불가)" if own and c.troops <= 0 else ""), f"벽{c.wall}"]
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
    allies = sorted({x for pair in state.alliances if faction in pair for x in pair} - {faction})
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
                  f" {o.stage} {o.progress:g}/{o.threshold:g} 병력{o.committed_troops} 사기{o.unit_morale}"
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


DISPOSITION_SYSTEM = """당신은 삼국지 시대 {faction}의 군주다. 사로잡은 포로 한 명의 처분을 정하라.

- 석방: 원 세력의 도시로 돌려보낸다(은혜를 베푸는 선택. 군주 포로를 풀어주면 군주로 복귀한다).
- 처형: 목을 벤다(위협 제거, 그러나 원한이 연혁에 남는다. 군주를 베면 그 세력은 새 군주를 세운다).
- 수감: 계속 가둬둔다 — 이후 달에 **명령(kind=설득)으로 등용을 시도**하거나, 상대가 몸값을 들고
  반환 협상을 걸어올 수 있다.

reason은 50자 이내 한국어 한 줄."""


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


def resolve_dispositions(state: GameState, verbose: bool = False) -> None:
    """턴 해소 직후 드라이버 훅: **신규 포획자만** 보유 세력 LLM에 즉결 질의(석방/처형/수감) → 엔진 적용.

    수감 선택 후엔 재질의 없음 — 이후는 설득 명령(kind=설득)·몸값 협상의 영역(⭐사용자 재설계).
    플레이어 모드는 이 함수 대신 결과 창이 그 세력 포로를 처리(Q4 결정함수 교체와 동형).
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
        d = decide_disposition(state, city, prisoner)
        if d is None:                                  # 호출 실패 → 다음 턴 재질의
            state.pending_captives.append((city, prisoner))
            continue
        if verbose:
            print(f"  [담화] {faction} → {prisoner}: {d.choice} ({d.reason})")
        apply_disposition(state, city, prisoner, d.choice)


# ======================= 외교: 제안 응답 질의 [[DISCUSSION#9-22]] =======================
class ProposalResponse(BaseModel):
    """외교 제안에 대한 상대 군주의 판단. 수락/거절은 이해득실 판단이라 확률(RNG) 없음 — 효과는 엔진."""
    accept: bool
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


PROPOSAL_SYSTEM = """당신은 삼국지 시대 {faction}의 군주다. {proposer}가 외교 제안을 보내왔다.
정세와 [주요 연혁](특히 상대의 과거 동맹 파기·배신 기록)을 보고 이해득실로 수락 여부를 판단하라.

- 동맹 제안: 수락하면 서로 공격할 수 없고, 서로의 도시를 구원 야전으로 도울 수 있다.
  어느 쪽이든 언제든 파기할 수 있다(파기는 기록에 남는다).
- 포로반환 요청: 수락하면 제시된 몸값(금·식량)을 받고 그 포로를 돌려보낸다.
  포로의 가치(능력·설득 가능성)와 몸값을 저울질하라.

reason은 50자 이내 한국어 한 줄."""


def decide_proposal_response(state: GameState, prop) -> ProposalResponse | None:
    """제안받은 세력 군주 LLM에 수락/거절 질의(소호출). 실패 시 None=보류(제안 잔존, 다음 턴 재질의)."""
    detail = (f"[제안] {prop.from_faction}의 동맹 제안" if prop.proposal == "동맹"
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


def resolve_proposals(state: GameState, verbose: bool = False) -> None:
    """턴 해소 직후 드라이버 훅: 대기 외교 제안을 상대 군주 LLM에 질의 → 엔진 적용.

    플레이어가 받는 제안은 이 함수 대신 결과 창에서 직접 응답(Q4 결정함수 교체와 동형, Streamlit 때).
    """
    from .engine import respond_proposal

    for prop in list(state.proposals):
        target = state.factions.get(prop.to_faction)
        if target is None or not target.alive:        # 그 사이 멸망 → 제안 소멸
            state.proposals.remove(prop)
            continue
        r = decide_proposal_response(state, prop)
        if r is None:
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
        advance_turn(state, actions)
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
