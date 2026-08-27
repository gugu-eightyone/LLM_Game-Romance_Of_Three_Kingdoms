"""작전지시(OpCommand) 테스트 — 회군(이동/교전)·전략변경·월권·회군+재출격 조합. 전부 오프라인."""
from src.models import Battle, City, Faction, GameState, General, OpCommand
from src.engine import advance_turn


def _state() -> GameState:
    """촉(성도) vs 위(업), 거리 3 = 여러 턴 이동을 강제(회군 타이밍 확보)."""
    return GameState(
        cities={
            "성도": City(name="성도", owner="촉", troops=10000, gold=5000, generals=["조운"]),
            "업":   City(name="업", owner="위", troops=8000, wall=2, generals=["조조"]),
        },
        factions={"촉": Faction(name="촉", ruler="유비"), "위": Faction(name="위", ruler="조조")},
        generals={"조운": General(name="조운", command=91, faction="촉"),
                  "조조": General(name="조조", command=96, is_ruler=True, faction="위")},
        distances={"성도": {"업": 3}, "업": {"성도": 3}},
    )


def _launch(s: GameState) -> int:
    advance_turn(s, {"촉": Battle(kind="전투", mode="공성", origin="성도", target="업",
                                   troops=6000, generals=["조운"], strategy="정공법")})
    return s.operations[0].id


def test_strategy_update():
    s = _state()
    oid = _launch(s)
    advance_turn(s, {"촉": OpCommand(kind="작전지시", op_id=oid, order="전략변경",
                                      strategy="서벽에 화공 집중")})
    op = next(o for o in s.operations if o.id == oid)
    assert op.action.strategy == "서벽에 화공 집중"
    assert any("[지시]" in h and "전략 갱신" in h for h in s.history)


def test_recall_while_moving_is_lossless():
    s = _state()
    oid = _launch(s)                                   # 1턴 이동(1/3), 아직 교전 아님
    advance_turn(s, {"촉": OpCommand(kind="작전지시", op_id=oid, order="회군")})
    assert s.operations == []
    assert s.cities["성도"].troops == 10000            # 무손실 복귀(4000 잔류 + 6000 귀환)
    assert "조운" in s.cities["성도"].generals


def test_recall_in_combat_pays_retreat_loss():
    s = _state()
    oid = _launch(s)
    advance_turn(s, [])                                # 이동 2/3
    advance_turn(s, [])                                # 3/3 도착 → 공성 교전 개시(+공성 1라운드 손실)
    op = next(o for o in s.operations if o.id == oid)
    assert op.stage == "교전"
    troops_before = op.committed_troops
    advance_turn(s, {"촉": OpCommand(kind="작전지시", op_id=oid, order="회군")})
    assert s.operations == [] or all(o.id != oid for o in s.operations)
    # 퇴각 손실 20%를 물고 돌아왔다(추가 교전 손실과 무관하게, 복귀 병력 < 교전 직전 병력의 100%)
    assert s.cities["성도"].troops < 4000 + troops_before
    assert any("퇴각 손실" in h for h in s.history)


def test_cannot_command_foreign_operation():
    s = _state()
    oid = _launch(s)
    advance_turn(s, {"위": OpCommand(kind="작전지시", op_id=oid, order="회군")})
    assert any(o.id == oid for o in s.operations)      # 작전 건재
    assert any("[위반]" in h and "남의 작전" in h for h in s.history)


def test_recall_then_redeploy_same_turn():
    """회군 + 새 출격을 한 턴에 = 작전 변경. 순서대로 즉시 처리라 성립."""
    s = _state()
    s.cities["한중"] = City(name="한중", owner="위", troops=1000)
    s.distances["성도"]["한중"] = 2
    s.distances["한중"] = {"성도": 2}
    oid = _launch(s)                                   # 업 방면 6000 출격(성도 잔여 4000)
    advance_turn(s, {"촉": [
        OpCommand(kind="작전지시", op_id=oid, order="회군"),          # 6000 즉시 귀환 → 성도 10000
        Battle(kind="전투", mode="공성", origin="성도", target="한중", troops=9000),
    ]})
    ops = [o for o in s.operations if o.action.target == "한중"]
    assert len(ops) == 1 and ops[0].committed_troops == 9000   # 회군 병력까지 합쳐 재출격됐다
    assert not any("과투입" in h for h in s.history)


def test_unknown_op_id_rejected():
    s = _state()
    advance_turn(s, {"촉": OpCommand(kind="작전지시", op_id=99, order="회군")})
    assert any("[기각]" in h and "작전99" in h for h in s.history)
