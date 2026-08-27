"""LLM_Game v2 — 구조화 출력 호출 래퍼 (신뢰 경계).

설계 근거: docs/DISCUSSION.md §8(신뢰경계·스택 결정).
- P1 해소: 맨손 `json.loads` 크래시 클래스를 0으로. LLM 출력이 뭐가 오든 앱이 안 죽는다.
- 스택: LangChain 없이 OpenAI SDK 직접. 구조화출력 = `chat.completions.parse(response_format=<Pydantic>)`.
  → SDK가 Pydantic을 strict json_schema로 변환, `.parsed`가 검증된 객체.
- 신뢰 경계는 "LLM 출력" 문턱 하나. 여기서만 검증, 안쪽 State는 통과(§8-2).
- 재시도 1회 → 폴백 1단계. 폴백 없으면 LLMError 하나로 수렴(호출자가 잡을 단일 예외).

관측성(토큰/비용/지연 F층)은 Phase 3 — 여기선 안 넣는다(YAGNI). usage는 필요할 때 반환값에 얹는다.
"""
from __future__ import annotations

import os
from typing import Callable, TypeVar

from openai import OpenAI
from pydantic import BaseModel

MODEL = "gpt-4.1-mini"   # 각국 결정·judge 공통(Q5). E층 검증 후 부족하면 승격.
TEMPERATURE = 0.2        # 0.7→0.2(2026-08-27): 규칙 준수·숫자 대조가 주 임무라 저온. 서사 붙일 때 그쪽만 고온 분리.

# T = "BaseModel을 상속한 어떤 Pydantic 클래스든" 자리 표시자.
# 넣은 스키마 그대로 반환 타입으로 이어짐 → 호출부가 캐스팅 없이 타입힌트 받음.
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """재시도까지 실패했는데 폴백도 없을 때 던지는 단일 예외.

    이 하나만 잡으면 되도록 수렴시킨다 — 호출부가 JSONDecodeError·APIError·
    ValidationError 등 잡다한 하류 예외를 개별로 신경 쓰지 않게(P1의 목적).
    """


# 클라이언트 하나를 모듈 전역에 재사용. import 시점에 만들면 키 없을 때 터지니 미룸.
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """지연 초기화. .env(루트)의 OPENAI_API_KEY를 SDK가 자동 사용."""
    global _client
    if _client is None:                       # 처음 호출 때 딱 한 번만 생성
        try:
            from dotenv import load_dotenv
            load_dotenv()            # 인자 없이 → 실행 위치부터 부모로 .env 탐색(§7)
        except ImportError:
            pass
        # 키를 안 읽고 존재 여부만 확인. 없으면 아리송한 SDK 에러 대신 우리 예외로.
        if not os.getenv("OPENAI_API_KEY"):
            raise LLMError("OPENAI_API_KEY 없음 — 루트 .env를 확인.")
        _client = OpenAI()                    # 키는 env에서 SDK가 알아서 집어감
    return _client


def _retrying(call: Callable[[], T], *, fallback: T | None, retries: int = 1) -> T:
    """thunk를 retries+1회 시도, 다 실패하면 폴백 or LLMError.

    call/fallback을 주입받는 순수 제어흐름 → 네트워크 없이 self-check 가능.
    """
    last: Exception | None = None
    for _ in range(retries + 1):          # retries=1 → 최초 1 + 재시도 1 = 총 2번
        try:
            return call()                 # 성공하면 즉시 반환, 아래 폴백까지 안 감
        except Exception as e:   # API·파싱·검증 등 무엇이든 여기서 흡수
            last = e                      # 마지막 실패 원인 보관(폴백 없을 때 붙임)
    # 여기 도달 = 다 실패. 폴백 있으면 그걸로 앱 살리고, 없으면 단일 예외로 수렴.
    if fallback is not None:
        return fallback
    raise LLMError(f"LLM 호출 {retries + 1}회 실패, 폴백 없음") from last  # 원인은 __cause__로


def structured_complete(
    response_format: type[T],
    system: str,
    user: str,
    *,
    fallback: T | None = None,
    model: str = MODEL,
    temperature: float = TEMPERATURE,
    retries: int = 1,
) -> T:
    """검증된 Pydantic 객체를 반환. 실패 시 폴백 or LLMError.

    response_format: 파싱 대상 Pydantic 모델 클래스.
    fallback: 다 실패했을 때 반환할 안전 기본값(예: 무해한 내정 Action). None이면 LLMError.
    """
    # 실제 API 한 번 때리는 몸통. thunk로 감싸 _retrying이 이걸 재시도하게.
    def _call() -> T:
        resp = _get_client().chat.completions.parse(
            model=model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=response_format,   # Pydantic → strict json_schema로 SDK가 변환
        )
        msg = resp.choices[0].message
        if msg.refusal:                        # 모델이 답 거부 → 예외로 올려 재시도/폴백 태움
            raise LLMError(f"모델 거부: {msg.refusal}")
        if msg.parsed is None:                 # 길이초과 등으로 파싱 실패 → 마찬가지로 예외
            raise LLMError("파싱 결과 없음(길이초과 등)")
        return msg.parsed                      # 여기 오면 검증 끝난 Pydantic 객체

    return _retrying(_call, fallback=fallback, retries=retries)


def demo() -> None:
    """제어흐름 self-check. 네트워크·API키 불필요. `python -m src.llm`."""
    class Toy(BaseModel):
        n: int

    ok, boom = Toy(n=1), Toy(n=-1)

    # 1) 첫 시도 성공 → 그대로 반환
    assert _retrying(lambda: ok, fallback=boom).n == 1

    # 2) 항상 실패 + 폴백 있음 → 폴백 반환(앱 안 죽음 = P1)
    def always_fail() -> Toy:
        raise ValueError("깨진 응답")
    assert _retrying(always_fail, fallback=boom).n == -1

    # 3) 실패 + 폴백 없음 → LLMError 단일 예외로 수렴(원인은 __cause__로 보존)
    try:
        _retrying(always_fail, fallback=None)
        raise AssertionError("LLMError가 나야 함")
    except LLMError as e:
        assert isinstance(e.__cause__, ValueError)

    # 4) 첫 시도 실패, 재시도 성공 → 성공값 반환(retries=1이 실제로 한 번 더 시도)
    calls = {"n": 0}
    def fail_then_ok() -> Toy:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 오류")
        return ok
    assert _retrying(fail_then_ok, fallback=None).n == 1 and calls["n"] == 2

    print("llm.py self-check 통과 [OK]")


if __name__ == "__main__":
    demo()
