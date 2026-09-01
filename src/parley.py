"""플레이어 담화(포로 설득) — 대화 루프 + 담화 심판. [[DISCUSSION#9-21]]

이원화의 플레이어 쪽: LLM 세력은 상수식(engine.persuade_chance)으로 즉결하지만,
플레이어는 포로와 실제로 몇 마디 나누고 그 담화 품질을 심판이 채점 → 확률로 환산한다.
= 첫 특화 심판 배선(C층 측정 표면 개시).

- 판정·효과는 전부 engine.apply_disposition(chance 주입)이 한다. 여긴 대화·채점만.
- UI는 Streamlit 때(st.write_stream). 지금은 CLI(`python -m src.parley`)와 오프라인 테스트로 검증.
- 페르소나 = 모델 내장지식(이름+원소속)만. 저작·RAG 없음(§9-7).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .config import PARLEY_MAX_ROUNDS, STRATEGY_MAX_CHARS
from .llm import LLMError, structured_complete
from .models import GameState
from .prompts import load as load_prompt


class ParleyReply(BaseModel):
    """포로의 대사 한 마디."""
    text: str = Field(max_length=200)


class ParleyScore(BaseModel):
    """담화 심판 채점(1~10, ⭐10점 통일 — 확률 11%p 간격의 세밀한 맛). 확률 환산은 score_to_chance(코드)."""
    score: int = Field(ge=1, le=10)
    reason: str = Field(default="", max_length=STRATEGY_MAX_CHARS)


# 프롬프트 원문은 prompts/*.txt (2026-08-30 이사)
PRISONER_SYSTEM = load_prompt("prisoner_persona")


def _loyalty_line(loyalty: int) -> str:
    """충의 수치 → 페르소나 연기 지시. 연기가 곧 난이도(⭐cap 폐지 — 숫자 천장 대신 인물이 어렵다, §9-21)."""
    if loyalty >= 90:
        return "당신의 충의는 강철이다. 상대가 아무리 옳은 말을 해도 사실상 넘어가지 않는다 — 흔들리는 연기조차 삼가라."
    if loyalty >= 70:
        return "당신은 충의가 깊다. 여간한 설득으로는 마음이 열리지 않지만, 명분이 정말 옳다면 아주 조금 흔들릴 수 있다."
    return "쉽게 넘어가지 마라 — 상대의 말이 당신의 처지·명분에 실제로 와닿을 때만 마음이 흔들린다."

JUDGE_SYSTEM = load_prompt("parley_judge")


def _fallen(state: GameState, g) -> bool:
    """⭐원 세력 멸망(도시 0) = 충의의 대상이 없음 → 충의 방패 해제(사용자 확정 2026-08-30)."""
    f = state.factions.get(g.faction) if g else None
    return f is None or not f.alive


def _script(transcript: list[tuple[str, str]]) -> str:
    return "\n".join(f"{who}: {line}" for who, line in transcript)


def prisoner_reply(state: GameState, city: str, prisoner: str,
                   transcript: list[tuple[str, str]]) -> str:
    """포로 페르소나의 응답 한 마디. 호출 실패 시 무해한 침묵 대사(앱 안 죽음)."""
    g = state.generals.get(prisoner)
    loyalty = ("당신이 섬기던 세력은 이미 멸망해 지킬 주군이 없다. 남은 것은 당신의 긍지와 앞날뿐 — "
               "명분과 예우가 닿으면 마음이 움직일 수 있다."
               if _fallen(state, g) else _loyalty_line(g.loyalty if g else 50))
    system = PRISONER_SYSTEM.format(prisoner=prisoner, faction=g.faction if g else "미상",
                                    captor=state.cities[city].owner,
                                    persona=f"\n당신의 사람됨: {g.persona}" if g and g.persona else "",
                                    loyalty=loyalty)
    user = _script(transcript) or "(군주가 당신을 바라본다)"
    if state.chronicle:
        user = "[주요 연혁]\n" + "\n".join(f"- {c}" for c in state.chronicle) + "\n\n" + user
    fb = ParleyReply(text="…… (말없이 고개를 돌린다)")
    return structured_complete(ParleyReply, system, user, fallback=fb).text


def judge_parley(state: GameState, city: str, prisoner: str,
                 transcript: list[tuple[str, str]]) -> ParleyScore:
    """담화 심판(특화 심판 1호). 실패 시 1점(공짜 성공 없음 — 안전 쪽으로 수렴)."""
    g = state.generals.get(prisoner)
    note = (f"{prisoner}가 섬기던 세력은 이미 멸망했다 — 충의의 대상이 없으니 충의 감점 없이 "
            f"명분·예우 중심으로 채점하라."
            if _fallen(state, g) else
            f"{prisoner}의 충의는 {g.loyalty if g else 50}/100이다. "
            f"충의가 높은 인물일수록 같은 논리도 낮게 받는다.")
    system = JUDGE_SYSTEM.format(prisoner=prisoner, faction=g.faction if g else "미상",
                                 captor=state.cities[city].owner, loyalty_note=note,
                                 persona=f" ({g.persona})" if g and g.persona else "")
    user = _script(transcript)
    if state.chronicle:
        user = "[주요 연혁]\n" + "\n".join(f"- {c}" for c in state.chronicle) + "\n\n" + user
    try:
        return structured_complete(ParleyScore, system, user)
    except LLMError:
        return ParleyScore(score=1, reason="심판 호출 실패 → 최저점")


def score_to_chance(score: int) -> float:
    """채점 1~10 → 설득 확률(⭐10점 통일). 1점=0, 10점=100%. ⭐상한 없음 — 인물 난이도는 심판 채점에 내재(§9-21)."""
    return (score - 1) / 9


def run_parley(state: GameState, city: str, prisoner: str,
               player_lines: list[str] | None = None, verbose: bool = True) -> bool:
    """담화 한 판: 최대 PARLEY_MAX_ROUNDS 왕복 → 심판 채점 → 엔진 판정. 반환=귀순 여부.

    player_lines가 있으면 그걸 소비(헤드리스·테스트), 없으면 input()(CLI).
    빈 줄 입력 = 담화 조기 종료(그때까지의 대화로 채점).
    """
    from .engine import attempt_persuade

    g = state.generals.get(prisoner)
    if g is not None and g.is_ruler and not _fallen(state, g):   # 현직 군주만 불가 — 망국 군주는 설득 가능(⭐사용자)
        if verbose:
            print(f"{prisoner}은(는) 일국의 군주 — 설득할 수 없다(석방/처형만).")
        return False
    transcript: list[tuple[str, str]] = []
    for i in range(PARLEY_MAX_ROUNDS):
        line = (player_lines[i] if player_lines and i < len(player_lines)
                else input("군주> ") if player_lines is None else "")
        if not line.strip():
            break
        transcript.append(("군주", line.strip()))
        reply = prisoner_reply(state, city, prisoner, transcript)
        transcript.append((prisoner, reply))
        if verbose:
            print(f"{prisoner}: {reply}")
    if not transcript:
        return False                                  # 한 마디도 안 함 = 시도 없음(차감도 없음)
    verdict = judge_parley(state, city, prisoner, transcript)
    chance = score_to_chance(verdict.score)           # 인물 난이도는 채점에 내재 — 상한 후처리 없음
    if verbose:
        print(f"[심판] {verdict.score}/10 ({verdict.reason}) → 확률 {chance:.0%}")
    ok = attempt_persuade(state, city, prisoner, chance)
    if verbose:
        print(f"[결과] {'귀순!' if ok else '설득 실패'}")
    return ok


def demo() -> None:
    """실 API CLI 담화 한 판. `python -m src.parley` — 수춘(위)에 감녕을 가두고 시작."""
    from .engine import load_scenario

    state = load_scenario()
    state.cities["시상"].generals.remove("감녕")
    state.cities["수춘"].prisoners.append("감녕")
    state.chronicle.append("0년 1월: 위, 오의 감녕 야전 포획")
    print("수춘(위)에 감녕이 수감됨. 빈 줄 = 담화 종료.")
    run_parley(state, "수춘", "감녕")


if __name__ == "__main__":
    demo()
