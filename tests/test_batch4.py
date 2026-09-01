"""배치 4(2026-09-02) — judge 배선·내정 담당 장수·동맹 기한제·수감 후 처분 동사. 전부 오프라인(가짜 judge)."""
import math

from src.config import DOMESTIC_GAIN, RECRUIT_CURVE_SCALE, STRATEGY_MODIFIER_BOUND
from src.engine import _judge_mod, advance_turn, apply_diplomacy, apply_domestic, respond_proposal
from src.models import Battle, City, Diplomacy, Dispose, Domestic, Faction, GameState, General, Proposal


def _state(seed: int = 0) -> GameState:
    return GameState(
        seed=seed,
        cities={
            "낙양": City(name="낙양", owner="위", troops=20000, gold=5000, food=8000, generals=["장료"]),
            "성도": City(name="성도", owner="촉", troops=15000, gold=9000, food=6000, generals=["조운"]),
            "건업": City(name="건업", owner="오", troops=15000, gold=4000, food=5000, generals=["여몽"]),
        },
        factions={"위": Faction(name="위", ruler="조조"), "촉": Faction(name="촉", ruler="유비"),
                  "오": Faction(name="오", ruler="손권")},
        generals={"장료": General(name="장료", command=95, intel=70, faction="위"),
                  "조운": General(name="조운", command=91, intel=76, faction="촉"),
                  "여몽": General(name="여몽", command=90, intel=88, faction="오")},
        distances={"낙양": {"성도": 1, "건업": 2}, "성도": {"낙양": 1, "건업": 1},
                   "건업": {"낙양": 2, "성도": 1}},
    )


# ---------- 심판 곡선(비대칭 로그, ⭐10점 통일: 중립=5) ----------
def test_judge_mod_curve():
    b = STRATEGY_MODIFIER_BOUND
    assert _judge_mod(10, b) == b                      # 10점 = +캡 도달(⭐"찍기 어렵지 않게")
    assert _judge_mod(5, b) == 0.0                     # 중립(일반론) = 무전략과 동일
    assert _judge_mod(1, b) == -b                      # 터무니없음 = -캡 클램프(무전략보다 손해)
    assert _judge_mod(2, b) == -b                      # 2점도 로그 급강하로 캡에 닿음
    assert 0 < _judge_mod(6, b) < _judge_mod(8, b) < b   # 6점≈+8%, 8점≈+20% — 단조 증가
    assert -b < _judge_mod(4, b) < 0                   # 4점 ≈ −10%
    assert _judge_mod(None, b) == 0.0                  # 심판 실패 = 보정 없음


# ---------- 전투 전략 채점 배선(가짜 judge) ----------
def test_strategy_judge_sets_mod_and_logs():
    s = _state()
    judge = lambda st, fac, kind, text: (10, "결정적 한 수")
    advance_turn(s, {"촉": [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                                    troops=10000, generals=[], strategy="파손된 북벽 집중 강습")]},
                 judge=judge)
    op = s.operations[0]
    assert op.strategy_mod == STRATEGY_MODIFIER_BOUND
    assert any("[심판]" in h and "10/10" in h for h in s.history)  # 가시화(점수·사유 노출)


def test_no_judge_or_empty_strategy_means_zero_mod():
    s = _state()
    advance_turn(s, {"촉": [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                                    troops=10000, generals=[], strategy="")]},
                 judge=lambda *a: (10, "호출되면 안 됨"))
    assert s.operations[0].strategy_mod == 0.0         # 무전략=채점 생략
    s2 = _state()
    advance_turn(s2, [Battle(kind="전투", mode="공성", origin="성도", target="낙양",
                             troops=10000, generals=[], strategy="아무 전략")])
    assert s2.operations[0].strategy_mod == 0.0        # judge 미주입(테스트·오프라인)=결정론 무변


# ---------- 모병: 대사 심판 + 담당 장수 ----------
def test_recruit_judge_and_overseer_multipliers():
    base = DOMESTIC_GAIN * RECRUIT_CURVE_SCALE * math.log1p(1000 / RECRUIT_CURVE_SCALE)
    s = _state()                                       # 담당 조운(통솔 91) + 심판 5점(+30%)
    apply_domestic(s, Domestic(kind="내정", city="성도", item="모병", gold_spent=1000,
                               general="조운", strategy="둔전병에게 후한 봉록을"),
                   judge=lambda st, fac, kind, text: (10, "탁월"))
    gained = s.cities["성도"].troops - 15000
    assert gained == round(base * (1 + 91 / 500) * 1.3)
    assert any("[심판]" in h and "효율 +30%" in h for h in s.history)
    s2 = _state()                                      # 무담당·무대사 = 곡선 기본값
    apply_domestic(s2, Domestic(kind="내정", city="성도", item="모병", gold_spent=1000))
    assert s2.cities["성도"].troops - 15000 == round(base)


def test_domestic_invalid_overseer_dropped():
    s = _state()
    apply_domestic(s, Domestic(kind="내정", city="성도", item="식량증산", gold_spent=1000,
                               general="장료"))        # 타국·타지 장수 → 제외, 배수 1.0
    assert s.cities["성도"].food == 6000 + 2000
    assert any("[환각]" in h and "담당" in h for h in s.history)


# ---------- 동맹 기한제 ----------
def test_alliance_expires_and_honorable_end():
    s = _state()
    s.alliances.append(("오", "촉"))
    s.alliance_expires["오|촉"] = 2
    s.alliances.append(("위", "촉"))                   # 엔트리 없음 = 무기한(하위호환)
    advance_turn(s, [])
    assert ("오", "촉") in s.alliances                 # 잔여 1개월
    advance_turn(s, [])
    assert ("오", "촉") not in s.alliances             # 만료
    assert ("위", "촉") in s.alliances                 # 무기한은 유지
    assert any("동맹 만료" in c for c in s.chronicle)  # 파기(배신)와 구분되는 명예 종료


def test_alliance_extension_resets_term():
    s = _state()
    s.alliances.append(("오", "촉"))
    s.alliance_expires["오|촉"] = 3
    apply_diplomacy(s, Diplomacy(kind="외교", target_faction="오", proposal="동맹", months=6),
                    actor="촉")                        # 동맹 중 재제안 = 연장 제안(기각 아님)
    assert len(s.proposals) == 1 and s.proposals[0].months == 6
    assert respond_proposal(s, s.proposals[0], True) is True
    assert s.alliance_expires["오|촉"] == 6            # 기한 재설정
    assert any("연장" in c for c in s.chronicle)


# ---------- 수감 후 처분 동사 ----------
def test_dispose_action_release_and_execute():
    s = _state()
    s.cities["낙양"].prisoners.extend(["조운", "여몽"])
    s.cities["성도"].generals.remove("조운")
    s.cities["건업"].generals.remove("여몽")
    advance_turn(s, {"위": [Dispose(kind="처분", city="낙양", prisoner="조운", choice="석방"),
                            Dispose(kind="처분", city="낙양", prisoner="여몽", choice="처형")]})
    assert "조운" in s.cities["성도"].generals          # 석방 → 원 세력 최근접 복귀
    assert "여몽" not in s.generals                     # 처형 → 로스터 제거
    assert any("석방" in c for c in s.chronicle) and any("처형" in c for c in s.chronicle)


def test_dispose_actor_guard():
    s = _state()
    s.cities["낙양"].prisoners.append("조운")
    advance_turn(s, {"촉": [Dispose(kind="처분", city="낙양", prisoner="조운", choice="석방")]})
    assert "조운" in s.cities["낙양"].prisoners         # 남의 감옥 → 월권 기각
    assert any("[위반]" in h and "처분" in h for h in s.history)
