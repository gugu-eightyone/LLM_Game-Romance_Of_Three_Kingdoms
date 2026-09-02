"""Streamlit 앱 스모크 — 오프라인(LLM 전부 폴백). 세팅→명령 추가→턴 종료→결과 창 한 바퀴."""
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _fake_complete(schema, system, user, *, fallback=None, **kw):
    """LLM 대역: 폴백 있으면 폴백(=decide 무해 내정), 없으면 None(=처분 유예·제안 보류)."""
    return fallback


def _click(at, label):
    next(b for b in at.button if label in b.label).click()
    return at.run(timeout=60)


def test_app_full_turn_offline():
    with patch("src.decide.structured_complete", side_effect=_fake_complete):
        # timeout 60: 콜드 임포트(streamlit+pandas)만 ~15s 나오는 환경(OneDrive·저사양 부하)이 실재
        at = AppTest.from_file(APP).run(timeout=60)
        assert not at.exception

        # 세팅 화면 → 기본값(위)으로 시작
        at = _click(at, "게임 시작")
        assert not at.exception and at.session_state["mode"] == "play"
        assert len(at.session_state["state"].cities) == 16

        # 내정 명령 추가(기본값 금 0 = 상태 무변)
        at.button(key="d_add").click()
        at = at.run(timeout=60)
        assert len(at.session_state["orders"]) == 1

        # 턴 종료 — AI 3세력은 폴백 명령, 앱 안 죽고 결과 창으로
        at = _click(at, "턴 종료")
        assert not at.exception and at.session_state["mode"] == "results"
        s = at.session_state["state"]
        assert (s.year, s.month) == (0, 2)

        # 결과 창 → 다음 달(플레이 화면 복귀)
        at = _click(at, "다음 달로")
        assert not at.exception and at.session_state["mode"] == "play"
