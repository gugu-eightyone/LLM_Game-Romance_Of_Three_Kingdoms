# 동적 스키마(⭐2026-09-02): 장수 필드 2곳(출격 동행·내정 담당)을 도시-장수 쌍 variant로 제약.
# 전부 오프라인 — 스키마 검증만(실 API 인식 여부는 스모크 몫).
import pytest
from pydantic import ValidationError

from src.decide import _decision_model
from src.engine import _chronicle, load_scenario


def _city_with_generals(state, faction):
    return next(n for n, c in state.cities.items() if c.owner == faction and c.generals)


def test_domestic_general_restricted():
    state = load_scenario()
    M = _decision_model(state, "위")
    city = _city_with_generals(state, "위")
    g = state.cities[city].generals[0]
    ok = M.model_validate({"actions": [
        {"kind": "내정", "city": city, "item": "모병", "gold_spent": 100, "general": g}]})
    assert ok.actions[0].general == g
    with pytest.raises(ValidationError):     # 타지/미존재 장수 = 스키마 수준에서 거부
        M.model_validate({"actions": [
            {"kind": "내정", "city": city, "item": "모병", "gold_spent": 100, "general": "관우"}]})


def test_domestic_unassigned_ok():
    state = load_scenario()
    M = _decision_model(state, "위")
    city = _city_with_generals(state, "위")
    ok = M.model_validate({"actions": [
        {"kind": "내정", "city": city, "item": "식량증산", "gold_spent": 100}]})
    assert ok.actions[0].general == ""       # 미지정(배수 1.0) 허용


def test_battle_origin_and_generals_restricted():
    state = load_scenario()
    M = _decision_model(state, "촉")
    city = _city_with_generals(state, "촉")
    g = state.cities[city].generals[0]
    ok = M.model_validate({"actions": [
        {"kind": "전투", "mode": "공성", "origin": city, "target": "장안",
         "troops": 100, "generals": [g]}]})
    assert ok.actions[0].generals == [g]
    with pytest.raises(ValidationError):     # 타지 장수 동행 거부
        M.model_validate({"actions": [
            {"kind": "전투", "mode": "공성", "origin": city, "target": "장안",
             "troops": 100, "generals": ["사마의"]}]})
    with pytest.raises(ValidationError):     # 적 도시 출발 거부(덤)
        M.model_validate({"actions": [
            {"kind": "전투", "mode": "공성", "origin": "업", "target": "장안",
             "troops": 100, "generals": []}]})


def test_empty_garrison_city_builds():
    state = load_scenario()
    city = _city_with_generals(state, "오")
    state.cities[city].generals = []
    M = _decision_model(state, "오")         # 장수 0 도시가 있어도 스키마 생성은 죽지 않는다
    ok = M.model_validate({"actions": [
        {"kind": "내정", "city": city, "item": "식량증산", "gold_spent": 0}]})
    assert ok.actions[0].general == ""
    with pytest.raises(ValidationError):     # 장수 0 도시의 담당 지정 = 전부 거부
        M.model_validate({"actions": [
            {"kind": "내정", "city": city, "item": "모병", "gold_spent": 0, "general": "주유"}]})


def test_chronicle_mirrors_history():
    state = load_scenario()
    _chronicle(state, "오, 촉과의 동맹 파기")
    assert any("파기" in c for c in state.chronicle)
    assert "[연혁] 오, 촉과의 동맹 파기" in state.history   # 턴 로그 무로그 구멍 수리
