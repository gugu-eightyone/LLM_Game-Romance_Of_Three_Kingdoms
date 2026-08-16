"""신뢰 래퍼의 재시도/폴백 self-check을 pytest로 편입(오프라인, API키 불필요)."""
from src.llm import demo


def test_llm_retry_fallback_selfcheck():
    demo()   # 재시도·폴백·LLMError 수렴을 가짜 함수로 검증(네트워크 없음)
