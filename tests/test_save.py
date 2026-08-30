"""세이브 직렬화 계약 — GameState JSON 왕복이 무손실인지(파일 I/O는 자명해서 제외)."""
from src.engine import advance_turn, load_scenario
from src.models import GameState


def test_gamestate_json_roundtrip():
    s = load_scenario()
    advance_turn(s, [])                                # 상태를 한 턴 굴리고(경제 틱 반영)
    s.alliances.append(tuple(sorted(("위", "촉"))))    # 튜플 필드 왕복 확인용
    s.pending_captives.append(("업", "감녕"))
    s2 = GameState.model_validate_json(s.model_dump_json())
    assert s2.model_dump() == s.model_dump()           # 무손실(튜플·중첩 모델 포함)
    assert s2.rng is not None                          # RNG는 시드로 재생성(진행 위치만 유실 — 설계 수용)
