"""프롬프트 로더 — 프롬프트 원문은 `prompts/*.txt`(2026-08-30 이사, 3개 이상 트리거 충족).

버저닝 = git 커밋 해시가 곧 버전(레포 안에 있으므로 별도 툴 불필요). 평가 결과에
"어느 프롬프트로 뽑았나"를 연결하는 용도. `{placeholder}`는 호출부가 .format()으로 채운다.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """prompts/<name>.txt 원문 반환(끝 개행 제거). 게임 중 불변이라 캐시."""
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n")
