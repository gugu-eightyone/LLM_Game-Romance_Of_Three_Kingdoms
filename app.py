"""LLM_Game v2 — Streamlit 앱 (플레이 + 관전). `streamlit run app.py`

Q4 결정: 플레이어 모드 = "그 세력 결정 함수만 사람으로 교체". 엔진·판정은 전부 src/(결정론),
여긴 위젯과 화면 전환뿐. 비대칭 원칙(§9-8): 플레이어=위젯으로 못 틀리게 / AI=자유 제안+가드.
- 처분 UI = 턴 종료 결과 창에서 3지선다(⭐사용자 2026-08-30). resolve_*(player=)가 큐를 남겨줌.
- 담화(설득) = 행동 턴에 시도, 명령 슬롯 자연 소모(§9-21). 로직은 parley.py 재사용.
- 외교 응답 = 버튼 + 옵션 대사 → AI 군주 응수 한 줄(⭐"묘미는 대화", 대사 입력 시에만=비용 옵트인).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import BaseModel, Field

from src.config import FOOD_ALERT_MONTHS, MAX_ORDERS_PER_TURN, PARLEY_MAX_ROUNDS
from src.decide import (brief, decide, matchup_judge, resolve_dispositions,
                        resolve_proposals, turn_judge)
from src.engine import (_city_threats, _wall_hp, _wall_max, advance_turn, allied,
                        apply_disposition, attempt_persuade, food_runway, load_scenario,
                        respond_proposal, surrender_gate)
from src.llm import LLMError, structured_complete
from src.models import Battle, Diplomacy, Dispose, Domestic, GameState, OpCommand, Transfer
from src.parley import (ParleyReply, _fallen, judge_parley, prisoner_reply,
                        score_to_chance)
from src.prompts import load as load_prompt

st.set_page_config(page_title="삼국지 LLM", page_icon="⚔️", layout="wide")

FACTIONS = ["위", "촉", "오"]


class Narration(BaseModel):
    """소설풍 턴 서사(순수 서사 — 상태 영향 0, 배치 평가 땐 끔)."""
    text: str = Field(max_length=600)


# ======================= 세션 헬퍼 =======================
SAVE_DIR = Path(__file__).parent / "saves"


def S() -> GameState:
    return st.session_state.state


def _boot(state: GameState, player: str | None, narrate: bool) -> None:
    """새 게임·로드 공용 세션 초기화(휘발 상태 전부 리셋 — 저장은 턴 경계라 잃을 게 없음)."""
    st.session_state.update(state=state, player=player, narrate=narrate,
                            orders=[], parley_used=0, events=[], retorts=[],
                            narration=None, over=None, mode="play", parley=None)


def new_game(player: str | None, seed: int, narrate: bool) -> None:
    s = load_scenario()
    s.seed = seed
    s._rng = None                                     # 시드 반영해 RNG 재생성
    _boot(s, player, narrate)


def save_game() -> Path:
    """턴 경계(명령 작성 화면)에서만 호출 — GameState + 세력/설정이면 판 전체가 복원된다.

    같은 (세력, 게임 날짜)면 덮어씀(퀵세이브 semantics). RNG 진행 위치는 저장 안 됨
    (시드로 재생성 — 플레이 무해, D층 완전 재현 필요해지면 getstate 동반 직렬화로 승격).
    """
    SAVE_DIR.mkdir(exist_ok=True)
    s = S()
    path = SAVE_DIR / f"{st.session_state.player or '관전'}_{s.year}년{s.month:02d}월.json"
    payload = {"player": st.session_state.player, "narrate": st.session_state.narrate,
               "state": s.model_dump(mode="json")}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_game(path: Path) -> None:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    _boot(GameState.model_validate(payload["state"]), payload["player"], payload["narrate"])


def orders_left() -> int:
    return MAX_ORDERS_PER_TURN - len(st.session_state.orders) - st.session_state.parley_used


def end_turn() -> None:
    s, player = S(), st.session_state.player
    h0, c0 = len(s.history), len(s.chronicle)
    ruler0 = s.factions[player].ruler if player else None
    actions: dict[str, list] = {}
    if player and st.session_state.orders:
        actions[player] = list(st.session_state.orders)
    for name, f in s.factions.items():
        if f.alive and name != player:
            with st.spinner(f"{name} 군주가 숙고 중..."):
                a = decide(s, name)
            if a:
                actions[name] = a
    with st.spinner("심판이 전략을 채점하고 전투가 벌어진다..."):
        # ⭐계획 단위 일괄 채점 + 교전 상성(2026-09-05) — 플레이어 전략도 같은 심판(가시화)
        advance_turn(s, actions, judge=turn_judge(s, actions), matchup=matchup_judge)
    with st.spinner("전후 처리(포로·외교)..."):
        resolve_dispositions(s, player=player)        # 플레이어 몫은 큐 잔존 → 결과 창이 소비
        resolve_proposals(s, player=player)
    st.session_state.update(events=s.history[h0:], orders=[], parley_used=0,
                            retorts=[], narration=None, mode="results")
    # 플레이어 패배 판정(엔진 승리조건 위에 플레이어 모드 전용 층, §9-10)
    if player:
        if s.winner:
            st.session_state.over = "승리! 천하통일" if s.winner == player else f"패배 — {s.winner} 천하통일"
        elif not s.factions[player].alive:
            st.session_state.over = "패배 — 세력 멸망"
        elif ruler0 and any(f", {ruler0} 처형" in c for c in s.chronicle[c0:]):  # 연혁 서식 "{처형자}, {이름} 처형" 정합 매칭(접미 이름 오탐 방지)
            st.session_state.over = f"패배 — 군주 {ruler0} 처형당함"
    elif s.winner:
        st.session_state.over = f"{s.winner} 천하통일"


# ======================= 화면: 새 게임 =======================
def setup_screen() -> None:
    st.title("⚔️ 삼국지 LLM 시뮬레이션")
    st.caption("숫자·규칙은 코드, 판단·서사는 LLM — 하이브리드 엔진 v2")
    who = st.radio("플레이 세력", FACTIONS + ["관전(AI 자율)"], horizontal=True)
    seed = st.number_input("시드(재현용)", value=0, step=1)
    narrate = st.toggle("소설풍 턴 서사(연의 어투, 턴당 LLM +1호출)", value=False)
    if st.button("게임 시작", type="primary"):
        new_game(None if who.startswith("관전") else who, int(seed), narrate)
        st.rerun()
    saves = sorted(SAVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True) if SAVE_DIR.exists() else []
    if saves:
        st.divider()
        pick = st.selectbox("세이브 불러오기", saves, format_func=lambda p: p.stem)
        if st.button("이어하기"):
            load_game(pick)
            st.rerun()


# ======================= 지도 (⭐UI 백로그: data/map.png + HTML 오버레이, 의존성 0) =======================
# 좌표 = 이미지(1568×1024) 성채 위치의 % 눈대중 — distances 그래프·강·세력색과 전수 대조로 확정.
# 이미지를 교체하면 이 표만 다시 잡으면 된다. 그림의 성채 색=시작 소유(정적) / 마커 색=현 소유(동적).
MAP_COORDS = {
    "업": (66.3, 14.6), "낙양": (40.5, 22.5), "장안": (31.9, 32.2), "허창": (48.8, 29.3),
    "완": (46.9, 43.0), "수춘": (64.7, 32.7), "하비": (75.9, 38.1), "양양": (59.0, 47.9),
    "한중": (30.0, 50.3), "성도": (27.1, 61.5), "강주": (31.9, 78.1), "강릉": (42.4, 67.9),
    "강하": (60.3, 66.4), "여강": (71.1, 59.1), "건업": (85.8, 60.5), "시상": (79.7, 76.7),
}
MARK_COLOR = {"위": "#4267b2", "촉": "#389e56", "오": "#c75450"}


@st.cache_data
def _map_b64() -> str | None:
    """지도 base64 — 원본 PNG 3.2MB는 st.markdown 델타로 매 rerun 실려 무거움 → 축소 JPEG(~0.2MB)로."""
    p = Path(__file__).parent / "data" / "map.png"
    if not p.exists():
        return None
    import base64
    import io

    from PIL import Image
    img = Image.open(p).convert("RGB")
    img.thumbnail((1200, 1200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def map_panel(s: GameState) -> None:
    img = _map_b64()
    if img is None:
        return
    edges = {frozenset((a, b)) for a, nbs in s.distances.items() for b in nbs
             if a in MAP_COORDS and b in MAP_COORDS}
    lines = []
    for a, b in (sorted(e) for e in edges):
        (x1, y1), (x2, y2) = MAP_COORDS[a], MAP_COORDS[b]
        lines.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')
        # 이정표 점 = 행군 월 단위 지점(교전 발생 위치). d개월 길 → 중간점 d-1개.
        d = s.distances.get(a, {}).get(b) or s.distances.get(b, {}).get(a) or 1
        for i in range(1, d):
            mx, my = x1 + (x2 - x1) * i / d, y1 + (y2 - y1) * i / d
            lines.append(f'<line class="ms" x1="{mx}" y1="{my}" x2="{mx}" y2="{my}"/>')
    roads = (f'<svg class="roads" viewBox="0 0 100 100" preserveAspectRatio="none">{"".join(lines)}</svg>')
    marks = []
    for n, (x, y) in MAP_COORDS.items():
        c = s.cities.get(n)
        if c is None:
            continue
        tip = (f"{n}({c.owner}) 병력 {c.troops:,} · 식량 {c.food:,} · 금 {c.gold:,}"
               + (f" · 장수 {','.join(c.generals)}" if c.generals else "")
               + (f" · 포로 {','.join(c.prisoners)}" if c.prisoners else ""))
        marks.append(
            f'<div class="ct" style="left:{x}%;top:{y}%" title="{tip}">'
            f'<span class="dot" style="background:{MARK_COLOR.get(c.owner, "#888")}"></span>'
            f'<span class="lb">{n}</span></div>')
    st.markdown(
        '<style>.mapwrap{position:relative;line-height:0}'
        '.mapwrap img{width:100%;border-radius:8px}'
        '.roads{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}'
        '.roads line{stroke:rgba(255,244,214,.4);stroke-width:1.5;vector-effect:non-scaling-stroke}'
        '.roads line.ms{stroke:rgba(255,244,214,.85);stroke-width:6;stroke-linecap:round}'
        '.ct{position:absolute;transform:translate(-50%,-110%);text-align:center;line-height:1;cursor:default}'
        '.dot{display:inline-block;width:11px;height:11px;border-radius:50%;border:2px solid #fff;'
        'box-shadow:0 0 4px rgba(0,0,0,.8)}'
        '.lb{display:block;margin-top:2px;font-size:12px;font-weight:700;color:#fff;'
        'text-shadow:0 0 3px #000,0 0 3px #000}</style>'
        f'<div class="mapwrap"><img src="data:image/png;base64,{img}">{roads}{"".join(marks)}</div>',
        unsafe_allow_html=True)
    st.caption("마커 색 = 현재 소유 세력(그림 성채 색은 개전 시점). 마커에 마우스를 올리면 상세.")


# ======================= 화면: 정세 =======================
def state_panel() -> None:
    s, player = S(), st.session_state.player
    cols = st.columns(len(FACTIONS) + 1)
    cols[0].metric("시간", f"{s.year}년 {s.month}월")
    for i, f in enumerate(FACTIONS, 1):
        fac = s.factions[f]
        n = sum(1 for c in s.cities.values() if c.owner == f)
        troops = sum(c.troops for c in s.cities.values() if c.owner == f) \
            + sum(o.committed_troops for o in s.operations if o.faction == f)
        label = f + (" (나)" if f == player else "") + ("" if fac.alive else " ☠")
        cols[i].metric(label, f"성 {n} · 병 {troops:,}", f"사기 {fac.morale} · 군주 {fac.ruler}",
                       delta_color="off")
    if s.alliances:
        st.caption("동맹: " + ", ".join(f"{a}-{b}" for a, b in s.alliances))
    # ⭐군량 경보(중요 정보 하이라이트): 현 소모율로 FOOD_ALERT_MONTHS개월 내 고갈되는 내 도시
    if player:
        alerts = [(n, r) for n, c in s.cities.items() if c.owner == player
                  and (r := food_runway(s, n)) is not None and r <= FOOD_ALERT_MONTHS]
        if alerts:
            st.warning("군량 경보 — " + ", ".join(
                f"{n}: " + ("이번 달 고갈 위험" if r == 0 else f"{r}개월 내 고갈") for n, r in alerts))
        # ⭐피침 하이라이트(UI 백로그): 군량 경보와 같은 결정론 술어를 빨간 배너로 — 표에 묻히지 않게
        threats = [(n, t) for n, c in s.cities.items() if c.owner == player
                   for t in _city_threats(s, n, player)]
        if threats:
            st.error("⚠ 피침 — " + ", ".join(
                f"{n}: {t.faction}군 {t.committed_troops:,}"
                + (" 성 앞 교전 중" if t.stage == "교전" else " 접근 중") for n, t in threats))

    with st.expander("전황 지도", expanded=True):
        map_panel(s)

    # ⭐도시 표 직관화(사용자 "엑셀표 밤티"): 세력별 바탕색 + 내 도시 우선 정렬
    _BG = {"위": "background-color: rgba(66,103,178,0.22)",
           "촉": "background-color: rgba(56,158,86,0.22)",
           "오": "background-color: rgba(199,84,80,0.22)"}
    ordered = sorted(s.cities.values(),
                     key=lambda c: (c.owner != player,
                                    FACTIONS.index(c.owner) if c.owner in FACTIONS else 9, c.name))
    df = pd.DataFrame([{
        "도시": c.name, "소유": c.owner, "레벨": c.level, "병력": c.troops,
        "성벽": f"{c.wall} ({_wall_hp(c)}/{_wall_max(c)})",
        "식량": c.food, "금": c.gold, "장수": ", ".join(c.generals),
        "포로": ", ".join(c.prisoners),
        "인접": ", ".join(f"{n}({d})" for n, d in s.distances.get(c.name, {}).items()),
    } for c in ordered])
    st.dataframe(df.style.apply(lambda r: [_BG.get(r["소유"], "")] * len(r), axis=1),
                 height=300, hide_index=True)

    if s.operations:
        st.markdown("**진행 중 작전**")
        for o in s.operations:
            # 진행도는 이동만 숫자. 공성 게이지=성벽 HP(도시 소유, ⭐HP화). 야전 교전=이동 잔여치 가분수라 무숫자.
            tgt = s.cities.get(o.action.target)
            phase = (f"공성 중 · 성벽 {_wall_hp(tgt)}/{_wall_max(tgt)}" if o.stage == "교전"
                     and o.action.mode == "공성" and tgt is not None
                     else "교전 중" if o.stage == "교전"
                     else f"이동 {o.progress:g}/{o.threshold:g}개월")
            # ⭐진행 게이지(사용자 "1/3 표기 맛없다"): 이동=진행률, 공성=성벽 파쇄율. 네이티브 st.progress로 충분.
            gauge = (min(1.0, o.progress / o.threshold) if o.stage == "이동" and o.threshold > 0
                     else 1 - _wall_hp(tgt) / _wall_max(tgt) if o.stage == "교전"
                     and o.action.mode == "공성" and tgt is not None and _wall_max(tgt) > 0
                     else None)
            line = (f"[{o.id}] {o.faction} {o.action.origin}→{o.action.target} {o.action.mode}"
                    f" · {phase} · 병력 {o.committed_troops} · 사기 {o.unit_morale}"
                    + (f" · 전략보정 {o.strategy_mod:+.0%}" if o.strategy_mod else "")
                    + (f" · 장수 {','.join(o.committed_generals)}" if o.committed_generals else ""))
            if player and o.faction == player:
                # ⭐작전지시 발견성(사용자 "조작 방도가 왜 없냐"): 내 작전 줄에 인라인 회군/전략변경.
                # 같은 명령은 작전지시 탭에서도 가능 — 여긴 지름길일 뿐(중복 큐는 버튼 비활성으로 방지).
                queued = any(getattr(a, "op_id", None) == o.id for a in st.session_state.orders)
                full = orders_left() <= 0
                c1, c2, c3 = st.columns([7, 1, 2])
                c1.text(line)
                if gauge is not None:
                    c1.progress(gauge)
                if c2.button("회군", key=f"op_r{o.id}", disabled=queued or full,
                             help="철수 명령을 큐에 담는다(교전 중=퇴각 손실)"):
                    st.session_state.orders.append(
                        OpCommand(kind="작전지시", op_id=o.id, order="회군"))
                    st.rerun()
                box = c3.popover("전략변경") if hasattr(st, "popover") else c3.expander("전략변경")
                with box:
                    txt = st.text_input("새 전략(50자)", key=f"op_s{o.id}", max_chars=50)
                    if st.button("하달", key=f"op_sb{o.id}", disabled=queued or full or not txt):
                        st.session_state.orders.append(
                            OpCommand(kind="작전지시", op_id=o.id, order="전략변경", strategy=txt))
                        st.rerun()
            else:
                st.text(line)
                if gauge is not None:
                    st.progress(gauge)
    with st.expander("주요 연혁"):
        st.text("\n".join(s.chronicle) or "(아직 없음)")
    with st.expander("최근 전황"):
        st.text("\n".join(s.history[-20:]) or "(아직 없음)")
    if player:
        with st.expander("정세 브리핑(LLM이 보는 그대로)"):
            st.text(brief(s, player))


# ======================= 화면: 명령 작성 =======================
_ORDER_ICON = {"전투": "⚔", "내정": "🏯", "호송": "🐎", "작전지시": "🚩", "외교": "🕊", "처분": "⚖", "설득": "🗣"}


def _order_line(a) -> str:
    """큐 명령 한 줄 요약(마찰 11: dict 원문 대신). 종류별 핵심만 — 상세는 어차피 방금 본 입력."""
    k = a.kind
    if k == "전투":
        body = f"{a.mode} {a.origin}→{a.target} · 병 {a.troops:,}" \
            + (f" · {a.generals[0]}" if a.generals else "") \
            + (f" · 「{a.strategy}」" if a.strategy else "")
    elif k == "내정":
        body = f"{a.item} {a.city} · 금 {a.gold_spent:,}" + (f" · 담당 {a.general}" if a.general else "") \
            + (f" · 「{a.strategy}」" if a.strategy else "")
    elif k == "호송":
        load = [x for x in (f"병 {a.troops:,}" if a.troops else "", " ".join(a.generals),
                            " ".join(f"포로 {p}" for p in a.prisoners),
                            f"금 {a.gold:,}" if a.gold else "", f"식량 {a.food:,}" if a.food else "") if x]
        body = f"{a.origin}→{a.target} · " + " · ".join(load or ["빈 수레"])
    elif k == "작전지시":
        body = f"작전{a.op_id} {a.order}" + (f" · 「{a.strategy}」" if a.strategy else "")
    elif k == "외교":
        body = f"{a.target_faction}에 {a.proposal}" \
            + (f" {a.months}개월" if a.proposal == "동맹" else "") \
            + (f" · {a.prisoner}" if getattr(a, "prisoner", "") else "")
    elif k == "처분":
        body = f"{a.prisoner} {a.choice} ({a.city})"
    else:
        body = a.model_dump_json(exclude_defaults=True)
    return f"{_ORDER_ICON.get(k, '•')} **{k}** — {body}"


def _clear_inputs(*keys: str) -> None:
    """명령 추가 직후 입력 위젯 초기화(마찰 10: 이전 입력 잔존 방지 — key 삭제=기본값 복귀)."""
    for k in keys:
        st.session_state.pop(k, None)


def order_builder() -> None:
    s, player = S(), st.session_state.player
    left = orders_left()
    st.subheader(f"이번 달 명령 ({MAX_ORDERS_PER_TURN - left}/{MAX_ORDERS_PER_TURN})")
    st.caption("처리 순서: 외교 → 전투 → 내정(같은 부류는 적은 순서대로). "
               "세력 간 순서는 공정성을 위해 매달 시드 랜덤. 모병한 병력은 다음 달부터 출격 가능.")
    for i, a in enumerate(st.session_state.orders):
        c1, c2 = st.columns([9, 1])
        c1.markdown(_order_line(a))                   # ⭐dict 원문 대신 읽히는 한 줄(마찰 11)
        if c2.button("✕", key=f"del{i}"):
            st.session_state.orders.pop(i)
            st.rerun()

    # ⭐큐에 담긴 장수는 후보에서 제외(마찰 9 — 한 장수 한 턴 한 임무, 엔진 규칙의 UI 가드)
    used_gens = {g for o in st.session_state.orders
                 for g in (*getattr(o, "generals", []),
                           *([o.general] if getattr(o, "general", "") else []))}
    free = lambda names: [n for n in names if n not in used_gens]
    # ⭐큐가 선점한 금·식량도 가용에서 차감(병력·장수와 같은 원칙 — 명령 결정 즉시 수치 갱신)
    q = st.session_state.orders
    q_gold = lambda c: (sum(a.gold_spent for a in q if a.kind == "내정" and a.city == c)
                        + sum(a.gold for a in q if a.kind == "호송" and a.origin == c))
    q_food = lambda c: sum(a.food for a in q if a.kind == "호송" and a.origin == c)

    mine = [n for n, c in s.cities.items() if c.owner == player]
    # ⭐st.tabs는 rerun마다 첫 탭 리셋(마찰 18) → 상태 유지되는 segmented_control
    tab = st.segmented_control("명령 종류", ["전투", "내정", "호송", "작전지시", "외교"],
                               key="order_tab", default="전투",
                               label_visibility="collapsed") or "전투"

    if tab == "전투":                                 # 전투: 출격 UI = LLM 명령서와 같은 양식(§TODO)
        armed = [n for n in mine if s.cities[n].troops > 0]
        if not armed:
            st.caption("출병 가능한 도시가 없다.")
        else:
            origin = st.selectbox("출발 도시", armed, key="b_o")
            city = s.cities[origin]
            # 이미 큐에 담긴 같은 도시 출격·호송 병력을 차감해 상한 표시(엔진 클램프에 기대지 않기)
            reserved = sum(getattr(o, "troops", 0) for o in st.session_state.orders
                           if getattr(o, "origin", None) == origin and o.kind in ("전투", "호송"))
            avail = max(0, city.troops - reserved)
            modes = ["공성", "야전"] + (["출성"] if _city_threats(s, origin, player) else [])
            mode = st.radio("종류", modes, horizontal=True, key="b_m",
                            help="야전=방면 요격/구원. 출성=피침 도시 성 앞 포진(수비대 분할).")
            if mode == "출성":
                target = origin
            else:
                adj = s.distances.get(origin, {})
                targets = [n for n in adj if s.cities[n].owner != player
                           and not allied(s, player, s.cities[n].owner)] if mode == "공성" \
                    else [n for n in adj if n != origin]
                target = st.selectbox("목표", targets, key="b_t") if targets else None
            troops = st.number_input(f"병력 (가용 {avail:,})", 1, max(1, avail),
                                     min(10000, max(1, avail)), key="b_n")
            # 장수 1명만(⭐사용자 2026-08-30): 추가 동행은 전투력 0 기여+포획 리스크뿐인 함정 선택지.
            # 장수 재배치는 호송 탭이 전용 동사. AI 경로는 리스트 유지(비대칭 원칙 — 위반 아님).
            gen = st.selectbox("지휘 장수", ["(없음)"] + free(city.generals), key="b_g",
                               help="부대 전투력 = 병력 × 장수 통솔 보정. 없이도 출격은 가능(보정 없음). "
                                    "이번 턴 다른 명령에 쓴 장수는 목록에서 빠진다(한 장수 한 턴 한 임무).")
            strat = st.text_input("전략(50자)", max_chars=50, key="b_s")
            if st.button("명령 추가", key="b_add", disabled=left <= 0 or target is None or avail <= 0):
                st.session_state.orders.append(Battle(
                    kind="전투", mode="야전" if mode == "출성" else mode, origin=origin,
                    target=target, troops=int(troops),
                    generals=[] if gen == "(없음)" else [gen], strategy=strat))
                _clear_inputs("b_n", "b_g", "b_s")
                st.rerun()

    elif tab == "내정":
        city_n = st.selectbox("도시", mine, key="d_c")
        item = st.radio("항목", ["식량증산", "모병", "사기진작", "성벽보수"], horizontal=True, key="d_i")
        d_avail = max(0, s.cities[city_n].gold - q_gold(city_n))
        gold = st.number_input(f"투입 금 (가용 {d_avail:,})",
                               0, d_avail, 0, key="d_g",
                               help="식량증산: 금 1→2. 모병: 금이 클수록 효율이 로그로 감소(도배 비추). "
                                    "성벽보수 = 파손 HP 복구(금 3당 1, 온전하면 기각). 사기진작은 80까지만(그 이상=금만 소모).")
        # ⭐담당 장수: 모병=통솔·식량/성벽=지력 비례 효율(안내 한 세트 — 사용자 확정)
        overseer = st.selectbox("담당 장수(선택)", ["(없음)"] + free(s.cities[city_n].generals), key="d_gen",
                                help="모병=통솔, 식량증산·성벽보수=지력에 비례해 효율 상승. 사기진작은 무관. "
                                    "이번 턴 다른 명령에 쓴 장수는 목록에서 빠진다.")
        strat = ""
        if item == "모병":
            strat = st.text_input("모병 방침(50자 — 심판이 채점해 효율 ±30%)", max_chars=50, key="d_s")
        elif item == "사기진작":
            strat = st.text_input("잔치 한마디(50자, 서사)", max_chars=50, key="d_s2")
        if st.button("명령 추가", key="d_add", disabled=left <= 0):
            st.session_state.orders.append(Domestic(
                kind="내정", city=city_n, item=item, gold_spent=int(gold),
                general="" if overseer == "(없음)" else overseer, strategy=strat))
            _clear_inputs("d_g", "d_gen", "d_s", "d_s2")
            st.rerun()

    elif tab == "호송":                               # 호송: 인접 아군 도시만(위젯이 걸러줌)
        pairs = [(o, t) for o in mine for t in s.distances.get(o, {}) if t in mine]
        if not pairs:
            st.caption("호송 가능한 아군 인접 경로가 없다.")
        else:
            origin = st.selectbox("출발", sorted({o for o, _ in pairs}), key="t_o")
            target = st.selectbox("도착", [t for o, t in pairs if o == origin], key="t_t")
            city = s.cities[origin]
            reserved = sum(getattr(o, "troops", 0) for o in st.session_state.orders
                           if getattr(o, "origin", None) == origin and o.kind in ("전투", "호송"))
            t_avail = max(0, city.troops - reserved)
            troops = st.number_input(f"병사 (가용 {t_avail:,})", 0, t_avail, 0, key="t_n",
                                     help="병사·물자·포로를 실으면 호위 최소 200. 장수 단독은 무호위 가능.")
            gens = st.multiselect("장수", free(city.generals), key="t_g")
            pris = st.multiselect("포로", city.prisoners, key="t_p")
            tg_avail = max(0, city.gold - q_gold(origin))
            tf_avail = max(0, city.food - q_food(origin))
            gold = st.number_input(f"금 (가용 {tg_avail:,})", 0, tg_avail, 0, key="t_gold")
            food = st.number_input(f"식량 (가용 {tf_avail:,})", 0, tf_avail, 0, key="t_f")
            # ⭐호송 규칙 티칭(마찰 8 — 사용자 문답으로 확정된 전체 그림)
            st.caption("호송은 길 위에선 호위대다 — 반대 방향 적과 마주치면 요격전(탑승 최고 통솔 장수가 지휘, "
                       "장수 수 스택 없음). 도착 즉시 수비대 합류·화물 하역. 목적지가 먼저 함락되면 화물째 회군.")
            if st.button("명령 추가", key="t_add", disabled=left <= 0):
                st.session_state.orders.append(Transfer(
                    kind="호송", origin=origin, target=target, troops=int(troops),
                    generals=gens, prisoners=pris, gold=int(gold), food=int(food)))
                _clear_inputs("t_n", "t_g", "t_p", "t_gold", "t_f")
                st.rerun()

    elif tab == "작전지시":
        ops = [o for o in s.operations if o.faction == player]
        if not ops:
            st.caption("진행 중인 우리 작전이 없다.")
        else:
            st.caption("작전지시도 명령 1회를 소모한다. 교전을 마친 출성·요격 부대는 상대가 "
                       "소멸하면 그 턴에 자동 복귀하니 회군 명령이 필요 없다.")   # ⭐마찰 16(즉시 복귀)
            label = {o.id: f"[{o.id}] {o.action.origin}→{o.action.target} {o.action.mode} ({o.stage})"
                     for o in ops}
            op_id = st.selectbox("작전", list(label), format_func=label.get, key="o_id")
            sel = next(o for o in ops if o.id == op_id)
            # ⭐호송은 전략이 없음(마찰 20): 적게 해놓고 기각하는 함정 제거 — 회군만 노출
            choices = ["회군"] if sel.action.mode == "호송" else ["전략변경", "회군"]
            order = st.radio("지시", choices, horizontal=True, key="o_ord",
                             help="회군: 교전 중이면 퇴각 손실. 전략변경: 전략문 교체(호송은 비전투 이동이라 전략 없음).")
            strat = st.text_input("새 전략(50자)", max_chars=50, key="o_s") if order == "전략변경" else ""
            if st.button("명령 추가", key="o_add", disabled=left <= 0):
                st.session_state.orders.append(OpCommand(
                    kind="작전지시", op_id=op_id, order=order, strategy=strat))
                _clear_inputs("o_s")
                st.rerun()

    elif tab == "외교":                               # 외교: 성립 가능한 제안만 노출(파기=동맹 중일 때만, ⭐§9-22)
        others = [f for f in FACTIONS if f != player and s.factions[f].alive]
        if not others:
            st.caption("남은 세력이 없다.")
        else:
            t = st.selectbox("상대", others, key="dp_t")
            opts = ["연장", "파기"] if allied(s, player, t) else ["동맹"]   # ⭐기한제: 동맹 중=연장 제안 가능
            my_captured = [p for c in s.cities.values() if c.owner == t
                           for p in c.prisoners if s.generals.get(p) and s.generals[p].faction == player]
            if my_captured:
                opts.append("포로반환")
            if surrender_gate(s, player, t):          # 게이트: 상대 도시 ≤2 + 국력 우위(엔진과 공유)
                opts.append("항복권유")
            prop = st.radio("제안", opts, horizontal=True, key="dp_p")
            months = 0
            if prop in ("동맹", "연장"):
                left_m = s.alliance_expires.get("|".join(sorted((player, t))))
                months = st.number_input("기한(개월)" + (f" — 현재 잔여 {left_m}개월" if left_m else ""),
                                         1, 60, 12, key="dp_mo",
                                         help="만료=자동 해소(명예 종료, 배신 아님). 연장 수락 시 이 기한으로 재설정.")
            prisoner, og, of, pay_city = "", 0, 0, ""
            if prop == "포로반환":
                prisoner = st.selectbox("되찾을 장수", my_captured, key="dp_pr")
                my_cities = [c.name for c in s.cities.values() if c.owner == player]
                pick = st.selectbox("몸값 지불 도시", ["자동(최대 보유 도시)"] + my_cities, key="dp_pc")
                pay_city = "" if pick.startswith("자동") else pick
                r_g = sum(getattr(a, "offer_gold", 0) for a in q if a.kind == "외교")
                r_f = sum(getattr(a, "offer_food", 0) for a in q if a.kind == "외교")
                base_g = (s.cities[pay_city].gold if pay_city
                          else max((c.gold for c in s.cities.values() if c.owner == player), default=0))
                base_f = (s.cities[pay_city].food if pay_city
                          else max((c.food for c in s.cities.values() if c.owner == player), default=0))
                max_g, max_f = max(0, base_g - r_g), max(0, base_f - r_f)
                og = st.number_input(f"몸값 금 (지불 가능 {max_g:,})", 0, max_g, 0, key="dp_g")
                of = st.number_input(f"몸값 식량 (지불 가능 {max_f:,})", 0, max_f, 0, key="dp_f")
            envoy, msg = "", ""
            if prop != "파기":
                my_gens = [g.name for g in s.generals.values() if g.faction == player]
                envoy = st.selectbox("사신(서사용)", [""] + my_gens, key="dp_e")
                msg = st.text_input("국서(50자)", max_chars=50, key="dp_m",
                                    help="⭐상대 군주가 국서를 읽고 수락/거절 판단에 실제로 반영한다 — "
                                         "빈 국서보다 설득력 있는 한 줄이 승인 확률을 높인다.")
            if st.button("명령 추가", key="dp_add", disabled=left <= 0):
                st.session_state.orders.append(Diplomacy(
                    kind="외교", target_faction=t,
                    proposal="동맹" if prop == "연장" else prop,   # 연장=동맹 재제안(엔진 규약)
                    months=int(months), prisoner=prisoner,
                    offer_gold=int(og), offer_food=int(of), pay_city=pay_city, envoy=envoy, message=msg))
                _clear_inputs("dp_m", "dp_g", "dp_f")
                st.rerun()

    # 포로 담화(설득)·후속 처분 — 담화=슬롯 즉시 소모(§9-21), 석방/처형=처분 명령(⭐배치4)
    held = [(c.name, p) for c in s.cities.values() if c.owner == player for p in c.prisoners]
    if held:
        st.markdown("**수감 중인 포로** — 담화(설득, 명령 1 소모) / 석방·처형(처분 명령 추가)")
        for city_n, p in held:
            g = s.generals.get(p)
            locked = g is not None and g.is_ruler and not _fallen(s, g)
            c1, c2, c3, c4 = st.columns([4, 1, 1, 1])
            c1.text(f"{p} ({city_n} 수감, 원 소속 {g.faction if g else '미상'})"
                    + (" — 현직 군주: 설득 불가" if locked else ""))
            if c2.button("담화", key=f"par_{city_n}_{p}", disabled=locked or left <= 0):
                st.session_state.parley = {"city": city_n, "prisoner": p,
                                           "transcript": [], "verdict": None}
                st.session_state.mode = "parley"
                st.rerun()
            for col, choice in ((c3, "석방"), (c4, "처형")):
                if col.button(choice, key=f"disp2_{city_n}_{p}_{choice}", disabled=left <= 0):
                    st.session_state.orders.append(Dispose(
                        kind="처분", city=city_n, prisoner=p, choice=choice))
                    st.rerun()

    st.divider()
    c1, c2 = st.columns([4, 1])
    if c1.button("🏁 턴 종료(이대로 진행)", type="primary"):
        end_turn()
        st.rerun()
    # ⭐저장 발견성(마찰 15): 사이드바에만 있던 걸 명령 화면에도 — 같은 퀵세이브
    if c2.button("💾 저장", key="save_main", help="턴 경계 상태 저장. 작성 중인 명령 큐는 저장되지 않는다."):
        st.toast(f"저장됨: {save_game().stem}")


# ======================= 화면: 담화 =======================
def parley_screen() -> None:
    s = S()
    ctx = st.session_state.parley
    city, prisoner = ctx["city"], ctx["prisoner"]
    st.subheader(f"담화 — {prisoner} ({city} 수감)")
    st.caption(f"최대 {PARLEY_MAX_ROUNDS}마디. 담화 종료 시 심판이 채점해 설득 확률이 된다.")
    for who, line in ctx["transcript"]:
        st.chat_message("user" if who == "군주" else "assistant").write(f"**{who}**: {line}")

    if ctx["verdict"] is None:
        rounds = sum(1 for w, _ in ctx["transcript"] if w == "군주")
        if rounds < PARLEY_MAX_ROUNDS:
            if line := st.chat_input("설득의 말..."):
                ctx["transcript"].append(("군주", line.strip()))
                with st.spinner(f"{prisoner}이(가) 입을 연다..."):
                    reply = prisoner_reply(s, city, prisoner, ctx["transcript"])
                ctx["transcript"].append((prisoner, reply))
                st.rerun()
        c1, c2 = st.columns(2)
        if c1.button("담화 종료(심판 채점)", disabled=not ctx["transcript"], type="primary"):
            with st.spinner("심판이 담화를 채점 중..."):
                v = judge_parley(s, city, prisoner, ctx["transcript"])
            chance = score_to_chance(v.score)
            ok = attempt_persuade(s, city, prisoner, chance)
            ctx["verdict"] = (v, chance, ok)
            st.session_state.parley_used += 1         # 명령 슬롯 자연 소모
            st.rerun()
        if c2.button("그만두기(무료)"):
            st.session_state.mode = "play"
            st.rerun()
    else:
        v, chance, ok = ctx["verdict"]
        st.info(f"심판 {v.score}/10 ({v.reason}) → 확률 {chance:.0%}")
        (st.success if ok else st.warning)("귀순!" if ok else "설득 실패 — 포로는 마음을 닫았다.")
        if st.button("돌아가기"):
            st.session_state.mode = "play"
            st.rerun()


# ======================= 화면: 턴 결과 창 =======================
def results_screen() -> None:
    s, player = S(), st.session_state.player
    st.subheader(f"{s.year}년 {s.month}월 — 지난 달 결과")
    if st.session_state.narrate and st.session_state.events:
        if st.session_state.narration is None:
            with st.spinner("사관이 붓을 든다..."):
                try:
                    st.session_state.narration = structured_complete(
                        Narration, load_prompt("narration"),
                        "\n".join(st.session_state.events)).text
                except LLMError:
                    st.session_state.narration = "(이 달의 기록은 소실되었다)"
        st.markdown(f"> {st.session_state.narration}")
    st.text("\n".join(st.session_state.events) or "(변화 없음)")
    for r in st.session_state.retorts:
        st.chat_message("assistant").write(r)

    if st.session_state.over:
        st.header(st.session_state.over)
        if st.button("새 게임"):
            st.session_state.mode = "setup"
            st.rerun()
        return

    # 처분 3지선다(⭐결과 창) — resolve_dispositions(player=)가 남긴 큐 소비
    my_captives = [(c, p) for c, p in s.pending_captives
                   if s.cities.get(c) and s.cities[c].owner == player]
    if my_captives:
        st.markdown("**새로 사로잡은 포로 처분** (설득은 다음 턴 담화로)")
        for city_n, p in my_captives:
            g = s.generals.get(p)
            c0, c1, c2, c3 = st.columns([3, 1, 1, 1])
            ruler = bool(g and g.is_ruler and s.factions.get(g.faction) and s.factions[g.faction].alive)
            c0.text(f"{p} ({city_n}, 원 소속 {g.faction if g else '미상'}"
                    + (", 군주! — 석방/처형만" if ruler else "") + ")")
            for col, choice in ((c1, "석방"), (c2, "수감"), (c3, "처형")):
                if choice == "수감" and ruler:        # ⭐군주=2지선다(§9-21 복원) — 수감 버튼 미노출
                    continue
                if col.button(choice, key=f"disp_{city_n}_{p}_{choice}"):
                    apply_disposition(s, city_n, p, choice)
                    s.pending_captives.remove((city_n, p))
                    st.rerun()

    # 외교 제안 응답(버튼=판정, 대사=서사 — ⭐대사 입력 시에만 AI 응수 소호출)
    my_props = [p for p in s.proposals if p.to_faction == player]
    for i, prop in enumerate(my_props):
        detail = {"동맹": "동맹 제안", "항복권유": "항복 권유 — 수락하면 나라를 접는다(패배)",
                  "포로반환": f"포로 {prop.prisoner} 반환 요청(몸값 금{prop.offer_gold}·식{prop.offer_food})"
                  }[prop.proposal]
        st.markdown(f"**{prop.from_faction}의 {detail}**"
                    + (f" · 사신 {prop.envoy}" if prop.envoy else "")
                    + (f"\n\n> {prop.message}" if prop.message else ""))
        say = st.text_input("답신 한마디(선택 — 상대가 읽고 응수한다)", max_chars=50,
                            key=f"say_{prop.from_faction}_{prop.proposal}_{prop.prisoner}")  # 인덱스 키=텍스트 상속 버그
        c1, c2 = st.columns(2)
        for col, accept in ((c1, True), (c2, False)):
            if col.button("수락" if accept else "거절", key=f"resp{i}_{accept}"):
                respond_proposal(s, prop, accept, say)
                if say:                               # 옵트인 응수: 판정 이후, 결과 영향 0
                    ruler = s.factions[prop.from_faction].ruler
                    fb = ParleyReply(text="…… (사신이 말없이 물러간다)")
                    r = structured_complete(ParleyReply, load_prompt("ruler_retort").format(
                        ruler=ruler, faction=prop.from_faction, target=player,
                        proposal=prop.proposal, verdict="수락" if accept else "거절"),
                        f"{player} 군주의 답신: {say}", fallback=fb)
                    st.session_state.retorts.append(f"**{ruler}**: {r.text}")
                if accept and prop.proposal == "항복권유":
                    st.session_state.over = f"패배 — {prop.from_faction}에 항복"
                st.rerun()

    st.divider()
    if st.button("다음 달로", type="primary"):
        st.session_state.mode = "play"
        st.rerun()


# ======================= 라우팅 =======================
if "mode" not in st.session_state:
    st.session_state.mode = "setup"

with st.sidebar:
    st.title("⚔️ 삼국지 LLM")
    if st.session_state.mode != "setup":
        who = st.session_state.player or "관전"
        st.caption(f"모드: {who} · 시드 {S().seed}")
        if st.session_state.mode == "play" and not st.session_state.over:
            if st.button("💾 저장"):                   # 턴 경계에서만(작성 중 명령 등 휘발 상태 없음)
                st.toast(f"저장됨: {save_game().stem}")
        if st.button("처음부터"):
            st.session_state.mode = "setup"
            st.rerun()

mode = st.session_state.mode
if mode == "setup":
    setup_screen()
elif mode == "parley":
    parley_screen()
elif mode == "results":
    results_screen()
else:                                                 # play
    state_panel()
    st.divider()
    if st.session_state.player:
        order_builder()
    else:
        if st.button("▶ 다음 턴 진행", type="primary"):
            end_turn()
            st.rerun()
    if st.session_state.over:                         # 관전 종료 등
        st.header(st.session_state.over)
