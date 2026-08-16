"""스키마 self-check을 pytest로 편입(인라인 demo 재사용)."""
from src.models import demo


def test_models_selfcheck():
    demo()   # 내부 assert들이 실패하면 pytest가 잡음
