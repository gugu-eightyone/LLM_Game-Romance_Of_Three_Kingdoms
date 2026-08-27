"""연혁(chronicle) 테스트 — 주요 사건이 영구 기록되고 brief에 실리는가. 전부 오프라인."""
from src.decide import brief
from src.models import Battle, City, Faction, GameState, General


def _state() -> GameState:
    """촉이 위의 유일 도시 '업'을 치면 함락→군주 포획(탈출로 0=확정)→멸망까지 한 턴에 간다."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=10000, gold=5000, generals=["조운"]),
            "업":   City(name="업", owner="위", troops=3000, generals=["조조"]),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91, faction="촉"),
                  "조조": General(name="조조", command=96, is_ruler=True, faction="위")},
        distances={"성도": {"업": 1}, "업": {"성도": 1}},
    )


def test_major_events_recorded_permanently():
    from src.engine import advance_turn
    s = _state()
    advance_turn(s, {"촉": Battle(kind="전투", mode="공성", origin="성도", target="업",
                                   troops=10000, generals=["조운"])})
    assert any("함락" in c for c in s.chronicle)
    assert any("군주 조조 포획" in c for c in s.chronicle)
    assert any("위 멸망" in c for c in s.chronicle)
    assert all(c.startswith("0년 1월:") for c in s.chronicle)   # 시점이 찍힌다


def test_brief_carries_chronicle_beyond_recent_history():
    """일반 history가 8줄 밀려나도 연혁은 brief에 남는다 — '장비의 원수'가 잊히지 않는 구조."""
    s = _state()
    s.chronicle.append("0년 1월: 위, 촉 장수 장비 처형")
    s.history.extend(f"잡담 {i}" for i in range(20))            # 최근 8줄 창을 밀어냄
    text = brief(s, "촉")
    assert "[주요 연혁]" in text and "장비 처형" in text
