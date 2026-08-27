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

from pydantic import BaseModel

from .llm import structured_complete
from .models import Action, Domestic, FactionName, GameState


class Decision(BaseModel):
    """구조화출력 래퍼. Union을 property로 내리기 위한 껍데기 한 겹."""
    action: Action


SYSTEM = """당신은 삼국지 시대 {faction}의 군주다. 주어진 정세를 읽고 이번 달 행동 하나를 정하라.

규칙(어기면 기각되어 한 달을 버린다):
- 전투는 반드시 **우리 도시**에서 출발하고, 목표는 그 도시의 **인접 목록에 있는 도시**여야 한다.
- 투입 병력은 출발 도시의 보유 병력 이내. 동행 장수는 그 도시에 주둔한 장수만.
- mode 공성 = **다른 세력의** 인접 도시를 점령한다(우리 도시는 공성 대상이 아니다). mode 야전 = 적 방면으로 출격해 진군 중인 적 부대를 길목에서
  요격한다(거리 2개월 이상인 도로에서만 마주친다).
- 내정은 우리 도시에 금을 투입한다. 식량증산·모병(금 1 → 2), 성벽보수, 사기진작.
- 이미 진행 중인 작전은 저절로 계속된다. 같은 작전을 다시 명령하지 마라(병력만 낭비된다).
- 계략(kind=계략)은 아직 미구현이니 절대 고르지 마라.
- strategy는 50자 이내 한국어 한 줄.

승리 조건은 천하통일이다. 다만 무리한 원정보다 전선 유지·국력 축적이 나은 달도 있다."""


def _city_line(state: GameState, name: str, own: bool) -> str:
    c = state.cities[name]
    parts = [f"병{c.troops}", f"벽{c.wall}"]
    if own:
        parts += [f"식{c.food}", f"금{c.gold}"]
    if c.generals:
        parts.append("장수 " + ",".join(f"{g}(통{state.generals[g].command})"
                                       if g in state.generals else g for g in c.generals))
    if c.prisoners:
        parts.append("포로 " + ",".join(c.prisoners))
    line = f"- {c.name}({c.owner}) " + "·".join(parts)
    if own:  # 인접은 우리 도시에만 붙인다 — 출병 가능한 목적지가 곧 이것이라(토큰 절약)
        adj = [f"{n}({state.cities[n].owner} {d}개월)"
               for n, d in state.distances.get(name, {}).items() if n in state.cities]
        line += " | 인접: " + ", ".join(adj)
    return line


def brief(state: GameState, faction: FactionName) -> str:
    """그 세력이 보는 정세 한 장. 난이도 보통=전 정보 공개(안개는 밀정 붙일 때)."""
    f = state.factions[faction]
    mine = [n for n, c in state.cities.items() if c.owner == faction]
    other = [n for n, c in state.cities.items() if c.owner != faction]
    lines = [f"[{state.year}년 {state.month}월] 우리={faction}, 군주={f.ruler}, 사기={f.morale}",
             "[우리 도시]", *[_city_line(state, n, True) for n in mine],
             "[타 세력 도시]", *[_city_line(state, n, False) for n in other]]
    if state.operations:
        lines.append("[진행 중 작전]")
        lines += [f"- [{o.id}] {o.faction} {o.action.origin}→{o.action.target} {o.action.mode}"
                  f" {o.stage} {o.progress:g}/{o.threshold:g} 병력{o.committed_troops} 사기{o.unit_morale}"
                  for o in state.operations]
    if state.history:
        lines += ["[최근 전황]", *[f"- {h}" for h in state.history[-8:]]]
    return "\n".join(lines)


def _fallback(state: GameState, faction: FactionName) -> Decision | None:
    """호출이 다 실패했을 때 반환할 무해한 행동 — 금 0 지출 내정(상태 무변, 로그만)."""
    mine = [n for n, c in state.cities.items() if c.owner == faction]
    if not mine:
        return None
    return Decision(action=Domestic(kind="내정", city=mine[0], item="식량증산", gold_spent=0))


def decide(state: GameState, faction: FactionName) -> Action | None:
    """한 세력의 이번 달 행동. 도시가 없으면 None(행동할 주체가 없음)."""
    fb = _fallback(state, faction)
    if fb is None:
        return None
    return structured_complete(
        Decision, SYSTEM.format(faction=faction), brief(state, faction), fallback=fb
    ).action


def decide_all(state: GameState) -> dict[FactionName, Action]:
    """살아있는 전 세력의 행동. `advance_turn(state, 이 dict)`에 그대로 넣으면 소유권까지 검증됨."""
    out: dict[FactionName, Action] = {}
    for name, f in state.factions.items():
        if not f.alive:
            continue
        a = decide(state, name)
        if a is not None:
            out[name] = a
    return out


def demo(turns: int = 6) -> None:
    """실 API로 자율 시뮬 몇 턴. `python -m src.decide [턴수]` (비용 = 3호출 × 턴수)."""
    from .engine import advance_turn, load_scenario

    state = load_scenario()
    for t in range(turns):
        h0 = len(state.history)
        actions = decide_all(state)
        for f, a in actions.items():
            print(f"  {f}: {a.model_dump_json(exclude_defaults=True)}")
        advance_turn(state, actions)
        print(f"[{state.year}년 {state.month}월 종료]")
        print("\n".join(state.history[h0:]) or "  (변화 없음)")
        if state.winner:
            print(f"★ 승자: {state.winner}")
            break


if __name__ == "__main__":
    import sys
    demo(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
