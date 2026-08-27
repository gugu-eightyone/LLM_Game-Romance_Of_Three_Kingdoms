"""호송(Transfer) + 한 턴 멀티 명령 테스트 (증분2). 전부 오프라인.

호송 = 병사·장수·포로·금·식량을 인접 아군 도시로. 호위 최소 200(장수 단독 면제).
멀티 명령 = 세력당 턴 상한 MAX_ORDERS_PER_TURN, 적힌 순서대로 처리.
"""
from src.models import Battle, City, Domestic, Faction, GameState, General, Transfer
from src.engine import advance_turn, load_scenario


def _state() -> GameState:
    """촉 2도시(성도↔한중 인접) + 위 1도시(업, 성도 인접). 성도에 위 포로 하후돈."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=10000, food=5000, gold=5000,
                          generals=["조운"], prisoners=["하후돈"]),
            "한중": City(name="한중", owner="촉", troops=1000, food=1000, gold=1000),
            "업":   City(name="업", owner="위", troops=3000, generals=["조조"]),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91, faction="촉"),
                  "조조": General(name="조조", command=96, is_ruler=True, faction="위"),
                  "하후돈": General(name="하후돈", command=89, faction="위")},
        distances={"성도": {"한중": 1, "업": 1}, "한중": {"성도": 1}, "업": {"성도": 1}},
    )


def test_transfer_arrives_and_merges():
    """거리1 호송: 같은 턴 도착, 병사·금·식량·장수·포로 전부 목적지에 합류·출발지 차감."""
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="한중", troops=1000,
                                     gold=500, food=300, generals=["조운"], prisoners=["하후돈"])})
    assert s.operations == []
    assert s.cities["한중"].troops == 2000 and s.cities["한중"].gold == 1500
    assert s.cities["한중"].food == 1300
    assert "조운" in s.cities["한중"].generals and "하후돈" in s.cities["한중"].prisoners
    assert s.cities["성도"].troops == 9000 and s.cities["성도"].gold == 4500
    assert s.cities["성도"].generals == [] and s.cities["성도"].prisoners == []


def test_escort_minimum_rejected():
    """물자를 실었는데 호위 병력 200 미만 → 기각, 아무것도 안 빠져나감."""
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="한중",
                                     troops=100, gold=500)})
    assert s.operations == []
    assert s.cities["성도"].troops == 10000 and s.cities["성도"].gold == 5000
    assert any("호위 부족" in h for h in s.history)


def test_general_solo_allowed():
    """장수만 이동(병력 0·화물 0)은 무호위 허용 — 군주 배치가 이 경로."""
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="한중", generals=["조운"])})
    assert "조운" in s.cities["한중"].generals
    assert s.cities["성도"].troops == 10000                # 병력 무변


def test_transfer_to_enemy_city_rejected():
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="업", troops=500)})
    assert s.operations == [] and s.cities["성도"].troops == 10000
    assert any("아군 도시 아님" in h for h in s.history)


def test_transfer_nonadjacent_rejected():
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="한중", target="업", troops=500)})
    assert s.operations == [] and any("비인접 호송" in h for h in s.history)


def test_transfer_actor_guard():
    """남의 도시에서 호송 지시 → 월권 기각."""
    s = _state()
    advance_turn(s, {"촉": Transfer(kind="호송", origin="업", target="성도", troops=500)})
    assert s.operations == [] and s.cities["업"].troops == 3000
    assert any("[위반]" in h and "호송" in h for h in s.history)


def test_orders_capped_per_turn():
    """LLM 경로(dict) 명령 5건 → 상한 4건까지만 처리 + [환각] 로깅."""
    s = _state()
    orders = [Domestic(kind="내정", city="성도", item="모병", gold_spent=100) for _ in range(5)]
    advance_turn(s, {"촉": orders})
    assert s.cities["성도"].gold == 5000 - 400             # 4건만 집행
    assert any("[환각]" in h and "상한" in h for h in s.history)


def test_orders_processed_in_sequence():
    """모병을 먼저 적으면 그 병력으로 같은 턴 출격 가능(순서대로 즉시 처리)."""
    s = _state()
    advance_turn(s, {"촉": [
        Domestic(kind="내정", city="성도", item="모병", gold_spent=1000),   # +2000 → 12000
        Battle(kind="전투", mode="공성", origin="성도", target="업", troops=12000),
    ]})
    assert any(op.committed_troops == 12000 for op in s.operations) or \
        any("함락" in h for h in s.history)                 # 12000 전부 투입됐다(클램프 안 걸림)
    assert not any("[환각]" in h and "과투입" in h for h in s.history)


def test_dest_captured_mid_transit_returns_home():
    """이동 중 목적지 함락 → 화물째 출발지로 회군."""
    s = _state()
    s.distances["성도"]["한중"] = 2                        # 2턴 걸리게
    s.distances["한중"]["성도"] = 2
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="한중",
                                     troops=1000, gold=500)})
    assert len(s.operations) == 1                          # 아직 이동 중
    s.cities["한중"].owner = "위"                          # 그 사이 함락
    advance_turn(s, [])
    assert s.operations == []
    assert s.cities["성도"].troops == 10000 and s.cities["성도"].gold == 5000  # 원상 복귀
    assert any("목적지 상실" in h for h in s.history)


def test_convoy_destroyed_loot_and_liberation():
    """전선화된 간선에서 호송대 전멸 → 금 노획(요격측 도시), 포로는 해방되어 원 세력 도시로."""
    s = _state()
    s.distances["성도"]["한중"] = 3
    s.distances["한중"]["성도"] = 3
    advance_turn(s, {"촉": Transfer(kind="호송", origin="성도", target="한중",
                                     troops=200, gold=500, prisoners=["하후돈"])})
    s.cities["한중"].owner = "위"                          # 목적지가 적성이 됨 → 간선=전선
    s.cities["한중"].troops = 20000
    advance_turn(s, [Battle(kind="전투", mode="야전", origin="한중", target="성도",
                            troops=20000)])                # 위 요격대, 같은 간선 반대방향
    # 다음 턴: progress 합(2+1=3) ≥ 거리 3 → 도로 교전 → 호위 200 전멸
    advance_turn(s, [])
    assert not any(op.action.mode == "호송" for op in s.operations)
    assert s.cities["한중"].gold == 1000 + 500             # 노획
    assert "하후돈" in s.cities["한중"].generals            # 해방 → 원 세력(위) 최근접 도시
    assert any("노획" in h for h in s.history) and any("해방" in h for h in s.history)


def test_roster_faction_derived_on_load():
    """시나리오 로드 시 장수 소속 = 시작 주둔 도시 소유주로 파생."""
    s = load_scenario()
    assert s.generals["조조"].faction == "위"
    assert s.generals["유비"].faction == "촉"
    assert s.generals["손권"].faction == "오"
