"""③ 확정 룰 3종(2026-09-10, 재플레이 트리아지): 치핑(마찰25)·포위 ZOC(마찰40)·고립 실체화(마찰49).

- 치핑: 열세 공성도 전력비만큼 성벽 최소 피해 — 함락 가드(우세도>0)는 유지.
- ZOC: 포위(교전 공성군) 중인 성에서 출발한 부대(전투·호송)는 공성군과 교전, 해소 전 전진 불가.
- 고립: 길 위 부대의 복귀·보급처 = 간선 양단 중 아군 노드만(적 성 관통 원격 보급 폐기).
"""
from src.config import SIEGE_CHIP_MAX, STRANDED_DESERTION
from src.engine import _home_city, advance_turn
from src.models import ActiveOperation, Battle, City, Faction, GameState, Transfer, Travel


def _state(**cities):
    names = list(cities)
    return GameState(
        cities={n: c for n, c in cities.items()},
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비"),
                  "오": Faction(name="오", ruler="손권")},
        distances={n: {m: 2 for m in names if m != n} for n in names},
    )


def _op(oid, faction, mode, origin, target, troops, stage="이동", progress=0.0, **kw):
    return ActiveOperation(
        id=oid, faction=faction, stage=stage, progress=progress, threshold=2,
        committed_troops=troops,
        action=Battle(kind="전투", mode=mode, origin=origin, target=target, troops=troops), **kw)


# ---------- 치핑 (마찰 25) ----------

def test_underdog_siege_chips_wall():
    """열세 공성(우세도<0)도 성벽이 조금씩 깎인다 — 구현 전엔 피해 0이었다."""
    s = _state(장안=City(name="장안", owner="촉", troops=12000, wall=2),
               낙양=City(name="낙양", owner="위", troops=1000))
    s.operations.append(_op(1, "위", "공성", "낙양", "장안", 8000, stage="교전"))
    full = s.cities["장안"].wall_hp if s.cities["장안"].wall_hp >= 0 else 4000
    advance_turn(s, [])
    assert s.cities["장안"].owner == "촉"                 # 함락 아님
    dmg = full - s.cities["장안"].wall_hp
    assert 0 < dmg <= SIEGE_CHIP_MAX                      # 전력비 비례 최소 치핑(호각 이하=CHIP_MAX 이하)


def test_breached_wall_underdog_still_no_capture():
    """성벽 0이어도 우세도≤0이면 함락 없음 — 치핑이 함락 가드를 깨지 않는다."""
    s = _state(장안=City(name="장안", owner="촉", troops=12000, wall=2, wall_hp=1),
               낙양=City(name="낙양", owner="위", troops=1000))
    s.operations.append(_op(1, "위", "공성", "낙양", "장안", 3000, stage="교전"))
    advance_turn(s, [])
    assert s.cities["장안"].wall_hp == 0
    assert s.cities["장안"].owner == "촉"


# ---------- 포위 ZOC (마찰 40) ----------

def _besieged_state():
    """하비(위)를 오가 교전 공성 중 + 위가 하비→허창으로 새 부대 출발."""
    s = _state(하비=City(name="하비", owner="위", troops=8000, wall=2),
               허창=City(name="허창", owner="촉", troops=3000),
               건업=City(name="건업", owner="오", troops=3000))
    s.operations.append(_op(1, "오", "공성", "건업", "하비", 6000, stage="교전"))
    s.operations.append(_op(2, "위", "공성", "하비", "허창", 2000))   # 이동 0/2 — 성문 앞
    return s


def test_departing_besieged_city_is_pinned_and_fights():
    s = _besieged_state()
    advance_turn(s, [])
    mover = next(o for o in s.operations if o.id == 2)
    assert mover.progress == 0                            # 전진 불가(교전 동결)
    assert mover.committed_troops < 2000                  # 성문 앞 교전 손실 발생
    assert any("작전2" in h and "야전" in h for h in s.history)


def test_departure_resumes_after_siege_lifted():
    s = _besieged_state()
    advance_turn(s, [])
    s.operations = [o for o in s.operations if o.id == 2]  # 공성군 소멸 가정
    advance_turn(s, [])
    mover = s.operations[0]
    assert mover.progress >= 1                            # 길이 열리면 행군 재개


def test_transfer_also_pinned_but_travel_exempt():
    s = _besieged_state()
    s.operations = [o for o in s.operations if o.id == 1]  # 공성군만 남기고
    s.operations.append(ActiveOperation(
        id=3, faction="위", stage="이동", progress=0, threshold=2, committed_troops=500,
        action=Transfer(kind="호송", origin="하비", target="허창", troops=500)))
    s.operations.append(ActiveOperation(
        id=4, faction="위", stage="이동", progress=0, threshold=2, committed_troops=0,
        committed_generals=["우금"],
        action=Travel(kind="개인이동", origin="하비", target="허창"), path=["하비", "허창"]))
    advance_turn(s, [])
    transfer = next(o for o in s.operations if o.id == 3)
    travel = next(o for o in s.operations if o.id == 4)
    assert transfer.progress == 0                         # 호송도 차단
    assert travel.progress >= 1                           # 개인 이동=완전 면제(단기 잠행)


# ---------- 고립 실체화 (마찰 49) ----------

def test_home_city_only_edge_endpoints():
    """양단이 다 적이면 인접에 아군 성이 있어도 고립 — 적 성 관통 보급·복귀 폐기."""
    s = _state(장안=City(name="장안", owner="촉", troops=5000),
               한중=City(name="한중", owner="촉", troops=5000),
               낙양=City(name="낙양", owner="위", troops=5000))   # 장안 인접 아군(구 폴백이 잡던 곳)
    op = _op(1, "위", "공성", "장안", "한중", 1000, progress=1.0)
    s.operations.append(op)
    assert _home_city(s, op) is None                      # 구 규칙이면 낙양이 나왔다
    op2 = _op(2, "위", "공성", "낙양", "한중", 1000)
    assert _home_city(s, op2) == "낙양"                   # 출발지가 아군이면 그대로


def test_stranded_op_starves_each_turn():
    s = _state(장안=City(name="장안", owner="촉", troops=5000, level=1, food=99999),
               한중=City(name="한중", owner="촉", troops=5000),
               낙양=City(name="낙양", owner="위", troops=5000, level=1, food=99999, gold=0))
    s.operations.append(_op(1, "위", "공성", "장안", "한중", 1000, progress=0.0))
    advance_turn(s, [])
    op = s.operations[0]
    assert op.committed_troops == 1000 - round(1000 * STRANDED_DESERTION)
    assert any("고립" in h and "탈영" in h for h in s.history)
