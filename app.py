"""LLM_Game v2 — Streamlit 앱 (플레이 + 관전). `streamlit run app.py`

Q4 결정: 플레이어 모드 = "그 세력 결정 함수만 사람으로 교체". 엔진·판정은 전부 src/(결정론),
여긴 위젯과 화면 전환뿐. 비대칭 원칙(§9-8): 플레이어=위젯으로 못 틀리게 / AI=자유 제안+가드.
- 처분 UI = 턴 종료 결과 창에서 3지선다(⭐사용자 2026-08-30). resolve_*(player=)가 큐를 남겨줌.
- 담화(설득) = 행동 턴에 시도, 명령 슬롯 자연 소모(§9-21). 로직은 parley.py 재사용.
- 외교 응답 = 버튼 + 옵션 대사 → AI 군주 응수 한 줄(⭐"묘미는 대화", 대사 입력 시에만=비용 옵트인).
- ⭐C묶음 UI(2026-09-05, docs/mockups/c_ui_mockup.html v18 확정 문법): 표시 계층=HTML/CSS 이식,
  입력부=위젯 유지. 카드 문법 = 「주체 헤더 + 세력 pill + 목적 pill + 작전번호 배지」 + 라벨 행.
  이모지는 경보(🔴⚠)·지도 ⚔·💾 저장만.
"""
from __future__ import annotations

import json
import re
import time
from html import escape as _esc
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import BaseModel, Field

from src.config import (CITY_INCOME_GOLD, FOOD_ALERT_MONTHS, MAX_ORDERS_PER_TURN,
                        PARLEY_MAX_ROUNDS)
from src.decide import (brief, decide, matchup_judge, resolve_dispositions,
                        resolve_proposals, turn_judge)
from src.engine import (_city_threats, _field_engagements, _wall_hp, _wall_max,
                        advance_turn, allied, apply_disposition, attempt_persuade,
                        food_net, food_runway, load_scenario, respond_proposal,
                        surrender_gate, travel_path)
from src.llm import LLMError, structured_complete
from src.models import (ActiveOperation, Battle, Diplomacy, Dispose, Domestic,
                        GameState, OpCommand, Transfer, Travel)
from src.parley import (ParleyReply, _fallen, judge_parley, prisoner_reply,
                        score_to_chance)
from src.prompts import load as load_prompt

st.set_page_config(page_title="삼국지 LLM", page_icon="⚔️", layout="wide")

FACTIONS = ["위", "촉", "오"]
FCOLOR = {"위": "#4267b2", "촉": "#389e56", "오": "#c75450"}


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
                            narration=None, over=None, mode="play", parley=None, eco=None)


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
    # ⭐회군=무료(D묶음, 마찰 23): 상한 카운트에서 제외(엔진과 동일 산식)
    billable = sum(1 for a in st.session_state.orders
                   if not (a.kind == "작전지시" and a.order == "회군"))
    return MAX_ORDERS_PER_TURN - billable - st.session_state.parley_used


def end_turn(slot=None) -> None:
    """턴 해소. slot(st.empty)이 오면 턴 종료 바 자리를 판정 진행 스텝으로 전환(⭐마찰 19)."""
    s, player = S(), st.session_state.player
    h0, c0 = len(s.history), len(s.chronicle)
    ruler0 = s.factions[player].ruler if player else None
    g0 = sum(c.gold for c in s.cities.values() if c.owner == player) if player else 0
    f0 = sum(c.food for c in s.cities.values() if c.owner == player) if player else 0
    t0 = time.monotonic()
    ai = [n for n, f in s.factions.items() if f.alive and n != player]
    steps = [f"{n} 군주 숙고" for n in ai] + ["심판·전투", "전후 처리"]

    def show(idx: int) -> None:
        if slot is not None:
            slot.markdown(_loadbar(f"{s.month}월 판정", steps, idx), unsafe_allow_html=True)

    actions: dict[str, list] = {}
    if player and st.session_state.orders:
        actions[player] = list(st.session_state.orders)
    for i, name in enumerate(ai):
        show(i)
        a = decide(s, name)
        if a:
            actions[name] = a
    show(len(ai))
    # ⭐계획 단위 일괄 채점 + 교전 상성(2026-09-05) — 플레이어 전략도 같은 심판(가시화)
    advance_turn(s, actions, judge=turn_judge(s, actions), matchup=matchup_judge)
    show(len(ai) + 1)
    resolve_dispositions(s, player=player)            # 플레이어 몫은 큐 잔존 → 결과 창이 소비
    resolve_proposals(s, player=player)
    eco = None
    if player:                                        # ⭐결과 창 하단 경제 요약(목업 ecoline)
        eco = (sum(c.gold for c in s.cities.values() if c.owner == player) - g0,
               sum(c.food for c in s.cities.values() if c.owner == player) - f0,
               time.monotonic() - t0)
    st.session_state.update(events=s.history[h0:], orders=[], parley_used=0,
                            retorts=[], narration=None, mode="results", eco=eco)
    # 플레이어 패배 판정(엔진 승리조건 위에 플레이어 모드 전용 층, §9-10)
    if player:
        if s.winner:
            st.session_state.over = "승리! 천하통일" if s.winner == player else f"패배 — {s.winner} 천하통일"
        elif not s.factions[player].alive:
            st.session_state.over = "패배 — 세력 멸망"
        elif ruler0 and any(f", {ruler0} 처형" in c for c in s.chronicle[c0:]):  # 연혁 서식 "{처분자}, {이름} 처형" 정합 매칭(접미 이름 오탐 방지)
            st.session_state.over = f"패배 — 군주 {ruler0} 처형당함"
    elif s.winner:
        st.session_state.over = f"{s.winner} 천하통일"


# ======================= C묶음 UI: 공통 CSS + HTML 조각 =======================
# 목업 v18 스타일 이식(다크 카드 — 라이트 테마에서도 카드 자체가 배경을 가져 안전).
_CSS = """<style>
:root{--panel:#171a21;--panel2:#1d212b;--line:#2a2f3a;--tx:#e8e6e3;--mut:#9aa0ab;--gold:#d4a94e}
.serif{font-family:"Noto Serif KR","Batang",serif}
.topbar{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin-bottom:8px;color:var(--tx)}
.brand{font-family:"Noto Serif KR","Batang",serif;font-size:17px;color:var(--gold);letter-spacing:2px}
.date{font-family:"Noto Serif KR","Batang",serif;font-size:19px;font-weight:700}
.date small{color:var(--mut);font-size:12px;font-weight:400;margin-left:6px}
.grow{flex:1}
.fpill{display:inline-flex;align-items:center;gap:6px;border-radius:20px;padding:2px 11px;font-weight:700;font-size:12.5px;color:#fff;white-space:nowrap;flex:none}
.fpill i{width:8px;height:8px;border-radius:50%;background:#fff;opacity:.85}
.slots{color:var(--mut);font-size:13px}.slots b{color:var(--tx)}
.facstrip{display:flex;gap:7px 14px;flex-wrap:wrap;align-items:center;margin-bottom:8px;color:var(--mut);font-size:12.5px}
.mapwrap{position:relative;line-height:0;border-radius:10px}
.mapwrap img{width:100%;display:block;border-radius:10px}
.roads{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.roads line{stroke:rgba(255,244,214,.38);stroke-width:1.5;vector-effect:non-scaling-stroke}
.roads line.river{stroke:rgba(120,180,255,.55)}
.roads line.ms{stroke:rgba(255,244,214,.85);stroke-width:6;stroke-linecap:round}
.ct{position:absolute;transform:translate(-50%,-110%);text-align:center;line-height:1;cursor:default}
.dot{display:inline-block;width:11px;height:11px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.8)}
.lb{display:block;margin-top:2px;font-size:12px;font-weight:700;color:#fff;text-shadow:0 0 3px #000,0 0 3px #000}
.pc{position:absolute;transform:translate(-50%,-50%);text-align:center;line-height:1;cursor:default}
.dia{display:inline-block;width:12px;height:12px;transform:rotate(45deg);border:2px solid #fff;box-shadow:0 0 5px rgba(0,0,0,.9)}
.plb{display:block;margin-top:4px;font-size:10.5px;font-weight:700;color:#ffe9b0;text-shadow:0 0 3px #000,0 0 3px #000}
.xw{position:absolute;transform:translate(-50%,-50%);font-size:13px;line-height:1;text-shadow:0 0 4px #000,0 0 4px #000;pointer-events:none}
.hcard{display:none;position:absolute;left:16px;top:-10px;width:254px;background:rgba(16,19,26,.96);border:1px solid var(--line);border-radius:10px;padding:9px 12px;line-height:1.5;font-size:12.5px;color:var(--tx);text-align:left;box-shadow:0 6px 24px rgba(0,0,0,.55);z-index:80}
.hcard.flip{left:auto;right:16px}
.hcard.up{top:auto;bottom:0}
.ct:hover .hcard,.pc:hover .hcard{display:block}
.ct:hover,.pc:hover{z-index:90}
.chead{display:flex;align-items:center;gap:7px;margin-bottom:5px;flex-wrap:wrap}
.chead b{font-family:"Noto Serif KR","Batang",serif;font-size:15px;white-space:nowrap;color:var(--tx)}
.badges{display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap}
.bdg{font-size:11px;border-radius:4px;padding:1px 7px;font-weight:700}
.bdg.war{background:rgba(224,101,92,.18);color:#ff9d95;border:1px solid rgba(224,101,92,.5)}
.bdg.food{background:rgba(212,169,78,.15);color:#e8c476;border:1px solid rgba(212,169,78,.45)}
.srow{display:flex;gap:12px;padding:1.5px 0;font-size:12.5px;color:var(--tx)}
.srow .k{color:var(--mut);flex:none;min-width:46px}
.vq{font-style:italic;color:var(--mut)}
.vj{color:#c8b98a}
.oid{font-size:10.5px;color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:0 5px;white-space:nowrap;flex:none}
.neg{color:#ff9d95}.pos{color:#8fd6a0}
.wbar{height:5px;border-radius:3px;background:#333a47;margin:3px 0 5px;overflow:hidden}
.wbar i{display:block;height:100%;background:linear-gradient(90deg,#b8905a,#d4a94e)}
.mpill{display:inline-block;border-radius:20px;padding:1px 9px;font-size:11px;font-weight:700;background:#333a47;color:#b7bdc8;border:1px solid #444c5c;white-space:nowrap;flex:none}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:1px 8px;font-size:11px;color:var(--tx)}
.evcols{display:grid;grid-template-columns:1.2fr 1fr;gap:10px;margin-top:8px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.panel h3{font-size:13px;color:var(--gold);margin:0 0 7px;font-family:"Noto Serif KR","Batang",serif}
.ev{padding:3px 0;font-size:13px;display:flex;gap:8px;align-items:baseline;color:var(--tx)}
.ev.big{color:#ffd98a;font-weight:700}
.ev .who{flex:none;width:8px;height:8px;border-radius:50%}
.evtag{font-size:11px;border:1px solid rgba(212,169,78,.5);border-radius:4px;padding:0 5px;color:var(--gold);flex:none}
.chron{color:var(--mut);font-size:12.5px;padding:2px 0}
.chron b{color:var(--tx);font-weight:400}
.op{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 14px;margin-top:8px}
.oprow{display:flex;align-items:center;gap:10px}
.opend{flex:none;font-weight:700;font-size:13px;color:var(--tx)}
.optrack{flex:1;position:relative;height:14px;border-radius:7px;background:#262c38;overflow:hidden}
.optrack i{position:absolute;inset:0 auto 0 0;border-radius:7px;background:linear-gradient(90deg,#b8905a,#d4a94e)}
.route{flex:1;position:relative;height:16px}
.route .ln{position:absolute;left:0;right:0;top:7px;height:2px;background:#3a4150}
.route .ln i{position:absolute;left:0;top:0;height:100%;background:rgba(255,244,214,.55)}
.route .nd{position:absolute;top:4px;width:8px;height:8px;border-radius:50%;background:#3a4150;border:1px solid #4a5261;transform:translateX(-50%)}
.route .nd.on{background:#c9c2b2;border-color:#e5ddc9}
.route .mk{position:absolute;top:2px;width:11px;height:11px;transform:translateX(-50%) rotate(45deg);border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.7)}
.route .cl{position:absolute;top:-11px;font-size:12px;transform:translateX(-50%);text-shadow:0 0 4px #000}
.opmeta{display:flex;justify-content:space-between;color:var(--mut);font-size:11.5px;margin-top:3px}
.opdet{border-top:1px dashed var(--line);margin-top:7px;padding-top:6px;color:var(--mut);font-size:12.5px}
.opdet b{color:var(--tx)}
.fold{color:var(--mut);font-size:11.5px}
.sub{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin-top:7px}
.phead{display:flex;align-items:center;gap:8px;font-family:"Noto Serif KR","Batang",serif;font-size:14px;color:var(--gold);margin:14px 0 4px}
.sdot{width:9px;height:9px;border-radius:50%;background:var(--gold);display:inline-block}
.sdot.off{background:#3a4150}
.qgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.qcard{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px;color:var(--tx)}
.qcard b{font-size:13px;color:var(--tx)}
.loadbar{display:flex;align-items:center;justify-content:center;gap:28px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;font-size:15px;color:var(--tx)}
.loadbar b{font-size:17px}
.step{color:var(--mut)}.step.done{color:#8fd6a0}.step.now{color:var(--gold);font-weight:700}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--gold);border-top-color:transparent;border-radius:50%;animation:sp 1s linear infinite;margin-right:7px;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.banner{background:linear-gradient(90deg,rgba(212,169,78,.16),rgba(212,169,78,.03));border:1px solid rgba(212,169,78,.5);border-radius:10px;padding:12px 16px;margin:10px 0;font-family:"Noto Serif KR","Batang",serif;font-size:15.5px;color:#ffd98a}
.fsec{margin-top:16px}
.rescard{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-top:8px;border-left:3px solid var(--line);color:var(--tx)}
.jchip{margin-left:auto;font-size:11.5px;border-radius:5px;padding:2px 8px;font-weight:700;background:rgba(143,214,160,.13);color:#8fd6a0;border:1px solid rgba(143,214,160,.4)}
.jchip.zero{background:rgba(154,160,171,.12);color:var(--mut);border-color:#3a4150}
.jchip.neg{background:rgba(224,101,92,.13);color:#ff9d95;border-color:rgba(224,101,92,.4)}
.jwhy{font-size:12.5px;color:#c8b98a;margin-top:2px}
.rline{font-size:13px;margin-top:5px;padding-left:10px;border-left:2px solid #2f3542}
.ecoline{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 14px;margin-top:14px}
/* ── 위젯 스타일 주입(⭐2026-09-05 사용자 "아예 맞추자" — 목업 주석의 'CSS 주입으로 통일' 실행) ── */
.block-container{max-width:1080px;padding-top:1.3rem}
h1,h2,h3{font-family:"Noto Serif KR","Batang",serif !important}
[data-testid="stSidebar"]{background:#171a21;border-right:1px solid var(--line)}
[data-testid="stSidebarCollapseButton"],[data-testid="collapsedControl"],[data-testid="stExpandSidebarButton"]{visibility:visible !important;opacity:.75}
[data-testid="stSidebarCollapseButton"] button,[data-testid="stExpandSidebarButton"] button{color:var(--mut)}
[data-testid="stVerticalBlockBorderWrapper"]{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px 10px}
.stButton button,[data-testid="stPopover"]>button{border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--tx)}
.stButton button:hover{border-color:var(--gold);color:var(--gold)}
.stButton button[kind="primary"],button[data-testid="stBaseButton-primary"]{font-family:"Noto Serif KR","Batang",serif;font-weight:700;font-size:16px;letter-spacing:1px;color:#1a1408;background:linear-gradient(180deg,#e0ba66,var(--gold));border:0}
.stButton button[kind="primary"]:hover,button[data-testid="stBaseButton-primary"]:hover{filter:brightness(1.07);color:#1a1408}
[data-testid="stExpander"] details,[data-testid="stExpander"]>details{background:var(--panel);border:1px solid var(--line);border-radius:10px}
[data-testid="stSegmentedControl"] button{border-radius:18px}
.stTextInput input,.stNumberInput input,[data-baseweb="select"]>div{border-radius:8px}
.stTextInput input:focus,.stNumberInput input:focus{border-color:var(--gold)}
/* ── 시작 화면 ── */
.hero{text-align:center;padding:40px 0 24px}
.hero .hbrand{font-family:"Noto Serif KR","Batang",serif;font-size:64px;line-height:1.1;color:var(--gold);letter-spacing:16px;text-indent:16px;text-shadow:0 2px 28px rgba(212,169,78,.30)}
.hero .hsub{font-family:"Noto Serif KR","Batang",serif;font-size:20px;font-weight:700;margin-top:12px;color:var(--tx)}
</style>"""


def H(x: str) -> None:
    st.markdown(x, unsafe_allow_html=True)


def _pill(faction: str, label: str = "", small: bool = False) -> str:
    style = f"background:{FCOLOR.get(faction, '#666')}" + (";font-size:11px;padding:1px 9px" if small else "")
    return f'<span class="fpill" style="{style}"><i></i>{label or faction}</span>'


def _mpill(t: str) -> str:
    return f'<span class="mpill">{t}</span>'


def _srow(k: str, v: str) -> str:
    return f'<div class="srow"><span class="k">{k}</span><span>{v}</span></div>'


def _man(troops: int) -> str:
    return f"{troops / 10000:.1f}만"


def _loadbar(title: str, steps: list[str], idx: int) -> str:
    parts = []
    for i, name in enumerate(steps):
        cls, mark = ("done", "✓ ") if i < idx else ("now", '<span class="spin"></span>') if i == idx else ("", "· ")
        parts.append(f'<span class="step {cls}">{mark}{name}</span>')
    return f'<div class="loadbar"><b class="serif" style="color:var(--gold)">{title}</b>{"".join(parts)}</div>'


# ======================= 룰북 (⭐사용자 2026-09-05: 실제 UI 요소를 예시로 띄우고 주석) =======================
# 화면 곳곳의 설명 캡션을 걷어내고 여기로 집약. 예시 카드=본 게임과 같은 CSS 클래스 재사용(가상 장면).
@st.dialog("룰북", width="large")
def rulebook() -> None:
    st.caption("각 항목은 실제 게임 화면의 요소를 예시로 함께 보여줍니다(예시의 수치는 임의값입니다).")

    H('<div class="phead">승리 조건</div>')
    st.markdown("지도의 **열여섯 성을 모두 차지한 세력**이 천하를 통일하고 승리합니다. "
                "모든 성을 잃은 세력은 멸망합니다. 플레이어는 군주가 처형당하거나 "
                "항복을 수락한 경우에도 패배합니다.")

    H('<div class="phead">상단 바</div>')
    H('<div class="topbar"><div class="brand">三國志</div>'
      '<div class="date">0년 3월<small>3턴째</small></div>'
      + _pill("위", "위 · 조조") +
      '<div class="grow"></div><span class="slots">명령 <b>2/4</b></span></div>')
    st.markdown("게임은 한 달 단위의 턴으로 진행됩니다. 명령을 작성한 뒤 「턴 종료」를 누르면 "
                "세 세력의 명령이 함께 집행됩니다.\n"
                "- 명령은 한 달에 최대 **4건**까지 내릴 수 있으며, 포로와의 담화도 1건으로 계산됩니다. "
                "단, **회군은 명령 횟수에 포함되지 않습니다.**\n"
                "- 명령은 **외교 → 전투 → 내정** 순서로 처리됩니다. 같은 분류 안에서는 작성한 순서를 따르며, "
                "세력 간의 처리 순서는 매달 무작위로 정해집니다.\n"
                "- 모병으로 충원한 병력은 **다음 달부터** 출격할 수 있습니다.\n"
                "- 💾 저장은 명령을 작성하기 전(달의 시작 시점)에만 가능하며, 작성 중인 명령은 저장되지 않습니다.")

    H('<div class="phead">지도 — 성 정보</div>')
    st.markdown("지도의 성 마커에 마우스를 올리면 상세 정보가 표시됩니다.")
    H('<div class="op" style="max-width:320px"><div class="chead"><b>하비</b>' + _pill("위", small=True) + "</div>"
      '<div class="badges"><span class="bdg war">🔴 피침 — 오군 12,000 접근 중</span>'
      '<span class="bdg food">⚠ 군량 2개월</span></div>'
      + _srow("병력", "18,200")
      + _srow("성벽", '3,549 ∕ 4,000 <span class="neg">(파손)</span>')
      + '<div class="wbar"><i style="width:88%"></i></div>'
      + _srow("금", '3,800 <span class="pos">(월수입 +900)</span>')
      + _srow("군량", '590 <span class="neg">(월수지 −210)</span>')
      + '<div class="chips"><span class="chip">장료 통95</span><span class="chip">장패 통80</span>'
      '<span class="chip" style="opacity:.6">포로 감녕</span></div></div>')
    st.markdown("- **성벽** — 성의 내구도입니다. 공성은 내구도를 깎으며, 내구도가 0인 상태에서 수비마저 "
                "밀리면 성이 함락됩니다. 파손된 성벽은 성벽보수(내정)로 복구하거나, 공격받지 않는 달에 소량씩 자연 회복됩니다.\n"
                "- **군량 월수지** — 병력은 매달 군량을 소비합니다. 수지가 적자인 상태에서 창고가 바닥나면 "
                "매달 병력이 이탈합니다. 모병으로는 이탈을 막을 수 없으며, 식량증산·호송으로 수지를 회복해야 합니다.\n"
                "- **장수** — 칩의 숫자는 통솔력입니다. 흐리게 표시된 칩은 이 성에 수감된 포로입니다.\n"
                "- **🔴 피침** — 적 부대가 이 성으로 접근 중이거나 교전 중임을 알리는 경보입니다.")

    H('<div class="phead">지도 — 부대 말</div>')
    st.markdown("길 위의 다이아 표식은 이동 중인 부대이며, 표식의 위치가 행군 진행도를 나타냅니다. "
                "⚔ 표식은 교전 지점입니다. 부대 표식에 마우스를 올리면 부대 정보가 표시됩니다.")
    H('<div class="op" style="max-width:320px"><div class="chead"><b>여몽 부대 '
      '<span style="color:var(--mut);font-weight:400;font-size:12px">(통90)</span></b>'
      + _pill("오", small=True) + _mpill("공성") + '<span class="oid">작전 4</span></div>'
      + _srow("경로", "건업 → 하비")
      + _srow("병력", '12,000 <span class="k">· 사기 82</span>')
      + _srow("상태", "공성 교전 중")
      + _srow("전략보정", '<span class="pos">+8%</span>') + "</div>")
    st.markdown("- 부대의 전투력은 **병력 수, 지휘 장수의 통솔, 부대의 사기**에 비례합니다. 지휘는 동행 장수 중 "
                "통솔이 가장 높은 1명이 맡으며, 장수를 여럿 동행시켜도 전투력은 오르지 않습니다(포획 위험만 늘어납니다). "
                "**지휘 장수가 없는 부대는 전투력이 낮아집니다**(수비하는 성도 마찬가지입니다).\n"
                "- 부대 사기는 교전할 때마다 낮아지며, 크게 떨어지면 부대가 강제로 퇴각할 수 있습니다.\n"
                "- 출격 중인 부대에는 병력을 추가할 수 없습니다. 증원이 필요하면 같은 목표로 부대를 "
                "추가 출격시키십시오(도착 시 협공이 됩니다).")

    H('<div class="phead">진행 중 작전</div>')
    H('<div class="op"><div class="chead" style="margin-bottom:4px"><b style="font-size:13.5px">서황 부대</b>'
      + _pill("위", small=True) + _mpill("출격") + '<span class="oid">작전 12</span>'
      '<div class="grow"></div><span class="fold">1/2개월</span></div>'
      '<div class="oprow"><span class="opend">허창</span>'
      '<div class="route"><div class="ln"><i style="width:50%"></i></div>'
      '<span class="nd on" style="left:0%"></span><span class="nd on" style="left:50%"></span>'
      '<span class="nd" style="left:100%"></span>'
      '<span class="mk" style="left:50%;background:#4267b2"></span></div>'
      '<span class="opend">수춘</span></div>'
      '<div class="opmeta"><span>병력 15,000 · 사기 60</span></div></div>')
    st.markdown("- 경로 바의 마디는 1개월 거리를 나타내며, 야전 교전은 이 지점들에서 발생합니다.\n"
                "- **길목 대기** — 거리 2개월 이상의 길에서는 부대를 중간 지점에 주둔시킬 수 있습니다. "
                "해당 길을 지나는 적 부대는 자동으로 요격되며, 교전 후에도 부대는 지점을 유지합니다. "
                "해제는 회군 명령으로 합니다.")
    H('<div class="op"><div class="chead" style="margin-bottom:4px"><b style="font-size:13.5px">하비</b>'
      + _pill("위", small=True) + _mpill("수성") + '<div class="grow"></div><span class="fold">수비 18,200</span></div>'
      '<div class="oprow"><span class="opend">성벽</span>'
      '<div class="optrack"><i style="width:89%"></i></div><span class="opend">3,549 ∕ 4,000</span></div>'
      '<div class="sub"><div class="chead"><b style="font-size:12.5px">장패 부대</b>'
      + _pill("위", small=True) + _mpill("출성") + "</div>"
      + _srow("병력", "6,000") + _srow("효과", "공성 견제 −25%") + "</div></div>")
    st.markdown("- **수성은 자동입니다.** 성에 주둔한 병력은 명령 없이도 성벽과 함께 방어합니다. "
                "단, 성벽은 주둔 병력이 있는 만큼만 방어력을 발휘합니다.\n"
                "- **출성** — 공격받는 성에서만 가능합니다. 수비대 일부가 성 밖에 포진해 적의 공성 속도를 "
                "늦추며, 위협이 사라지면 자동으로 성에 복귀합니다. 병력을 과하게 내보내면 성의 방어가 약해집니다.")

    H('<div class="phead">명령 내리기</div>')
    H('<div class="qgrid"><div class="qcard"><div class="chead"><b>출격</b>' + _mpill("전투") + "</div>"
      + _srow("경로", "허창 → 수춘") + _srow("장수", "서황") + _srow("병력", "15,000")
      + _srow("전략", '<span class="vq">「함락 직후의 혼란을 틈타 즉시 탈환한다」</span>') + "</div>"
      '<div class="qcard"><div class="chead"><b>모병</b>' + _mpill("내정") + "</div>"
      + _srow("도시", "하비") + _srow("담당", "장료") + _srow("투입", "금 3,000") + "</div></div>")
    st.markdown("- **전투** — 공성(인접한 적의 성 공격) / 야전(길 위의 적 부대와 교전 — 적 방면은 요격, "
                "아군 방면은 구원) / 출성 / 길목 대기.\n"
                "- **내정** — 식량증산(금을 식량으로 바꿉니다), 모병(투입 금액이 클수록 효율이 떨어집니다), "
                "성벽보수(파손된 성벽을 복구합니다), 사기진작(잔치로 올릴 수 있는 사기에는 상한이 있습니다). "
                "담당 장수를 지정하면 효율이 오릅니다 — "
                "모병은 통솔, 증산·보수는 지력을 따릅니다. 담당은 해당 성에 주둔한 장수만 가능하며, "
                "**한 장수는 한 달에 하나의 임무만** 수행할 수 있습니다.\n"
                "- **호송** — 병력·물자·포로·장수를 인접한 아군 성으로 운송합니다. 적재물이 있으면 호위 병력 "
                "200 이상이 필요하며, 이동 중 적과 마주치면 교전이 발생합니다. **왕복**을 선택하면 도착 후 "
                "적재물만 내려놓고 호위 장수가 출발지로 복귀합니다(복귀는 개인 이동으로 처리되어 안전합니다).\n"
                "- **개인 이동** — 장수 1명을 아군 영토를 경유해 원거리 아군 성까지 이동시킵니다. "
                "교전·요격·포획의 대상이 되지 않습니다.\n"
                "- **작전지시** — 진행 중인 작전에 전략변경(재채점, 명령 1건) 또는 회군(명령 횟수 미포함, "
                "교전 중에는 퇴각 손실 발생)을 지시합니다.\n"
                "- **외교** — 동맹(기한제 — 만료는 자연 종료, 파기는 즉시 효력이나 배신으로 기록됩니다), "
                "포로반환(몸값 협상), 항복권유(상대 세력이 성 2개 이하이고 국력이 열세일 때만 가능). "
                "국서의 내용은 상대 군주의 판단에 실제로 반영됩니다.\n"
                "- **담화** — 수감한 포로의 등용을 시도합니다. 최대 3마디의 대화 후 심판이 채점하며, "
                "채점 결과가 설득의 성패를 좌우합니다. 재위 중인 군주는 설득할 수 없습니다(세력이 멸망한 경우는 예외).")

    H('<div class="phead">심판</div>')
    H('<div class="rescard" style="border-left-color:#4267b2">'
      '<div class="chead"><b style="font-size:13.5px">[작전12] 위 허창→수춘 진군 개시(병력 15000)</b>'
      '<span class="jchip">심판 7/10 → +8%</span></div>'
      '<div class="jwhy">[심판] 작전12 전략 「함락 직후의 혼란을 틈타 즉시 탈환한다」 7/10 '
      '(수비 정비 전 타격은 시점 판단이 구체적) → 전투 보정 +8%</div></div>')
    st.markdown("전투 전략·모병 방침·담화는 심판이 1~10점으로 채점하며, 점수는 전투력 보정·모병 효율·"
                "설득 확률로 반영됩니다. **현재 정세에 근거한 구체적인 서술만 가점**을 받습니다 — 일반론은 "
                "보정이 없고, 정세와 어긋난 전략은 오히려 감점됩니다. 전황이 바뀌면 전략변경으로 갱신하십시오"
                "(다시 채점됩니다). 교전에서 양측의 전략이 명확히 맞물리는 경우, **상성 판정**이 한쪽에 "
                "추가 보정을 부여합니다.\n\n"
                "결과 화면의 금색 배너는 **연혁**입니다. 함락·배신·처형 등 주요 사건은 영구 기록되며, "
                "각 세력 군주의 이후 판단에 계속 인용됩니다.")


# ======================= 화면: 새 게임 =======================
def setup_screen() -> None:
    # ⭐게임 디자인 통일(사용자 2026-09-05): 히어로 타이틀 + 중앙 폼 — 카드 문법과 같은 serif·gold 톤
    H('<div class="hero"><div class="hbrand">三國志</div>'
      '<div class="hsub">삼국지 LLM 시뮬레이션</div></div>')
    _, mid, _ = st.columns([1, 1.7, 1])
    with mid:
        who = st.radio("플레이 세력", FACTIONS + ["관전(AI 자율)"], horizontal=True)
        narrate = st.toggle("소설풍 턴 서사(연의 어투, 턴당 LLM +1호출)", value=False,
                            help="게임 중에도 사이드바에서 켜고 끌 수 있습니다.")
        with st.expander("고급 설정"):                 # ⭐시드=고급으로(백로그: 시작 화면 정리)
            seed = st.number_input("시드(재현용)", value=0, step=1)
        if st.button("게임 시작", type="primary", use_container_width=True):
            new_game(None if who.startswith("관전") else who, int(seed), narrate)
            st.rerun()
        if st.button("📖 룰북", use_container_width=True):
            rulebook()
        saves = sorted(SAVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True) if SAVE_DIR.exists() else []
        if saves:
            with st.expander("세이브 불러오기"):
                pick = st.selectbox("세이브", saves, format_func=lambda p: p.stem,
                                    label_visibility="collapsed")
                if st.button("이어하기", use_container_width=True):
                    load_game(pick)
                    st.rerun()


# ======================= 상단 바 + 세력 요약 =======================
def topbar() -> None:
    s, player = S(), st.session_state.player
    turn = s.year * 12 + s.month
    c1, c2 = st.columns([11, 1])
    with c1:
        who = _pill(player, f"{player} · {s.factions[player].ruler}") if player \
            else '<span class="mpill">관전</span>'
        used = MAX_ORDERS_PER_TURN - orders_left() if player else 0
        slots = (f'<span class="slots">명령 <b>{used}/{MAX_ORDERS_PER_TURN}</b></span>'
                 if player else "")
        H(f'<div class="topbar"><div class="brand">三國志</div>'
          f'<div class="date">{s.year}년 {s.month}월<small>{turn}턴째</small></div>'
          f'{who}<div class="grow"></div>{slots}</div>')
    if c2.button("💾", help="달의 시작 시점을 저장합니다. 작성 중인 명령은 저장되지 않습니다."):
        st.toast(f"저장됨: {save_game().stem}")
    strip = []
    for f in FACTIONS:
        fac = s.factions[f]
        n = sum(1 for c in s.cities.values() if c.owner == f)
        troops = sum(c.troops for c in s.cities.values() if c.owner == f) \
            + sum(o.committed_troops for o in s.operations if o.faction == f)
        tag = " (나)" if f == player else ("" if fac.alive else " ☠")
        strip.append('<span style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap">'
                     + _pill(f, f + tag, small=True)
                     + f"<span>성 {n} · 병 {troops:,} · 사기 {fac.morale} · 군주 {fac.ruler}</span></span>")
    H(f'<div class="facstrip">{"".join(strip)}</div>')


# ======================= 지도 (⭐C묶음: 호버 카드 + 부대 말 + 교전 ⚔) =======================
# 좌표 = 이미지(1568×1024) 성채 위치의 % 눈대중 — distances 그래프·강·세력색과 전수 대조로 확정.
# 이미지를 교체하면 이 표만 다시 잡으면 된다. 그림의 성채 색=시작 소유(정적) / 마커 색=현 소유(동적).
MAP_COORDS = {
    "업": (66.3, 14.6), "낙양": (40.5, 22.5), "장안": (31.9, 32.2), "허창": (48.8, 29.3),
    "완": (46.9, 43.0), "수춘": (64.7, 32.7), "하비": (75.9, 38.1), "양양": (59.0, 47.9),
    "한중": (30.0, 50.3), "성도": (27.1, 61.5), "강주": (31.9, 78.1), "강릉": (42.4, 67.9),
    "강하": (60.3, 66.4), "여강": (71.1, 59.1), "건업": (85.8, 60.5), "시상": (79.7, 76.7),
}


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


def _hcard(x: float, y: float, inner: str) -> str:
    """마커에 붙는 호버 카드 — 우측·하단 가장자리는 반대쪽으로 펼침."""
    cls = "hcard" + (" flip" if x > 62 else "") + (" up" if y > 68 else "")
    return f'<div class="{cls}">{inner}</div>'


def _city_hover(s: GameState, c) -> str:
    """도시 호버 카드(⭐마찰 2 "게임 같지 않아"): 경보 배지 + 성벽 바 + 월수지 + 장수 칩."""
    head = (f'<div class="chead"><b>{c.name}</b>'
            + (_pill(c.owner, small=True) if c.owner in FCOLOR else _mpill("중립")) + "</div>")
    badges = []
    if c.owner in FACTIONS:
        for t in _city_threats(s, c.name, c.owner):
            badges.append(f'<span class="bdg war">🔴 피침 — {t.faction}군 {t.committed_troops:,} '
                          + ("교전 중" if t.stage == "교전" else "접근 중") + "</span>")
        r = food_runway(s, c.name)
        if r is not None and r <= FOOD_ALERT_MONTHS:
            badges.append(f'<span class="bdg food">⚠ 군량 {r}개월</span>')
    hp, mx = _wall_hp(c), _wall_max(c)
    net = food_net(s, c.name)
    rows = [_srow("병력", f"{c.troops:,}"),
            _srow("성벽", f"{hp:,} ∕ {mx:,}" + ('<span class="neg"> (파손)</span>' if hp < mx else "")),
            f'<div class="wbar"><i style="width:{0 if mx == 0 else hp / mx * 100:.0f}%"></i></div>',
            _srow("금", f"{c.gold:,}" + (f'<span class="pos"> (월수입 +{c.level * CITY_INCOME_GOLD:,})</span>'
                                        if c.level > 0 else "")),
            _srow("군량", f"{c.food:,}" + ("" if net is None else
                                          f'<span class="{"pos" if net >= 0 else "neg"}"> (월수지 {net:+,})</span>'))]
    chips = [f'<span class="chip">{g} 통{s.generals[g].command}</span>'
             for g in c.generals if g in s.generals]
    chips += [f'<span class="chip" style="opacity:.6">포로 {p}</span>' for p in c.prisoners]
    return (head + (f'<div class="badges">{"".join(badges)}</div>' if badges else "")
            + "".join(rows) + (f'<div class="chips">{"".join(chips)}</div>' if chips else ""))


def _op_kind(o: ActiveOperation) -> str:
    if o.action.mode == "개인이동":
        return "개인 이동"
    if o.action.mode == "호송":
        return "호송"
    if o.action.origin == o.action.target:
        return "출성"
    if getattr(o.action, "hold_at", 0):
        return "길목 대기"
    if o.stage == "이동":
        return "출격"
    return o.action.mode


def _op_name(s: GameState, o: ActiveOperation) -> str:
    if o.committed_generals:
        g = max(o.committed_generals, key=lambda n: s.generals[n].command if n in s.generals else 0)
        cmd = f' <span style="color:var(--mut);font-weight:400;font-size:12px">(통{s.generals[g].command})</span>' \
            if g in s.generals else ""
        return f"{g} 부대{cmd}"
    return f"{o.faction}군 부대"


def _op_hover(s: GameState, o: ActiveOperation, foes: list[ActiveOperation]) -> str:
    head = (f'<div class="chead"><b>{_op_name(s, o)}</b>{_pill(o.faction, small=True)}'
            f'{_mpill(_op_kind(o))}<span class="oid">작전 {o.id}</span></div>')
    rows = []
    if o.action.origin == o.action.target:
        rows.append(_srow("위치", f"{o.action.origin} 성 앞"))
    elif len(o.path) >= 2:                            # 개인 이동=경유 도시열 그대로
        rows.append(_srow("경로", " → ".join(o.path)))
    else:
        rows.append(_srow("경로", f"{o.action.origin} → {o.action.target}"))
    rows.append(_srow("병력", f'{o.committed_troops:,} <span class="k">· 사기 {o.unit_morale}</span>'))
    hold = getattr(o.action, "hold_at", 0)
    rows.append(_srow("상태", "교전 중" if o.stage == "교전"
                      else f"{hold}개월 지점 대기 중" if hold and o.progress >= hold
                      else f"이동 {o.progress:g}/{o.threshold:g}개월"))
    for foe in foes:
        rows.append(_srow("상대", f"{foe.faction} {_op_name(s, foe).split(' <')[0]} {foe.committed_troops:,}"))
    if o.strategy_mod:
        rows.append(_srow("전략보정", f'<span class="{"pos" if o.strategy_mod > 0 else "neg"}">'
                          f"{o.strategy_mod:+.0%}</span>"))
    return head + "".join(rows)


def _op_positions(s: GameState) -> dict[int, tuple[float, float]]:
    """작전 → 지도 % 좌표. 이동=진행률 지점, 공성=성에 붙음, 출성=성문 밖(공성군 방향)."""
    pos = {}
    for o in s.operations:
        a, b = o.action.origin, o.action.target
        if o.action.mode == "개인이동" and len(o.path) >= 2:   # ⭐다구간 경로: 진행분만큼 구간 따라 보간
            left = o.progress
            for p, q in zip(o.path, o.path[1:]):
                d = s.distances.get(p, {}).get(q) or s.distances.get(q, {}).get(p) or 1
                if left <= d or (p, q) == (o.path[-2], o.path[-1]):
                    if p in MAP_COORDS and q in MAP_COORDS:
                        (x1, y1), (x2, y2) = MAP_COORDS[p], MAP_COORDS[q]
                        f = min(0.94, max(0.06, left / d))
                        pos[o.id] = (x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
                    break
                left -= d
            continue
        if a not in MAP_COORDS or b not in MAP_COORDS:
            continue
        (x1, y1), (x2, y2) = MAP_COORDS[a], MAP_COORDS[b]
        if a == b:                                    # 출성: 공성군 쪽으로 성문 밖 한 발
            foe = next((p for p in s.operations if p is not o and p.action.target == a
                        and p.faction != o.faction and p.action.origin in MAP_COORDS
                        and p.action.origin != a), None)
            if foe:
                (fx, fy) = MAP_COORDS[foe.action.origin]
                f = 0.10
                pos[o.id] = (x1 + (fx - x1) * f, y1 + (fy - y1) * f)
            else:
                pos[o.id] = (x1, y1 + 4)
            continue
        if o.stage == "교전":
            f = 0.90                                  # 공성·구원 교전: 목표 도시 코앞
        else:
            f = min(0.94, max(0.06, o.progress / o.threshold if o.threshold else 0))
        pos[o.id] = (x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
    return pos


def map_panel(s: GameState) -> None:
    img = _map_b64()
    if img is None:
        return
    edges = {frozenset((a, b)) for a, nbs in s.distances.items() for b in nbs
             if a in MAP_COORDS and b in MAP_COORDS}
    lines = []
    for a, b in (sorted(e) for e in edges):
        (x1, y1), (x2, y2) = MAP_COORDS[a], MAP_COORDS[b]
        river = " river" if any({a, b} == set(e) for e in s.river_edges) else ""
        lines.append(f'<line class="{river.strip()}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')
        # 이정표 점 = 행군 월 단위 지점(교전 발생 위치). d개월 길 → 중간점 d-1개.
        d = s.distances.get(a, {}).get(b) or s.distances.get(b, {}).get(a) or 1
        for i in range(1, d):
            mx, my = x1 + (x2 - x1) * i / d, y1 + (y2 - y1) * i / d
            lines.append(f'<line class="ms" x1="{mx}" y1="{my}" x2="{mx}" y2="{my}"/>')
    roads = f'<svg class="roads" viewBox="0 0 100 100" preserveAspectRatio="none">{"".join(lines)}</svg>'

    marks = []
    for n, (x, y) in MAP_COORDS.items():
        c = s.cities.get(n)
        if c is None:
            continue
        marks.append(f'<div class="ct" style="left:{x}%;top:{y}%">'
                     f'<span class="dot" style="background:{FCOLOR.get(c.owner, "#888")}"></span>'
                     f'<span class="lb">{n}</span>{_hcard(x, y, _city_hover(s, c))}</div>')

    # ⭐부대 말(마찰 7) + 교전 ⚔ (지도 위 국면: 공성=성에 붙음, 출성=성문 밖 대치, 야전=길 위 맞붙음)
    pos = _op_positions(s)
    pairs = _field_engagements(s)
    foes: dict[int, list[ActiveOperation]] = {}
    for a, b in pairs:
        foes.setdefault(a.id, []).append(b)
        foes.setdefault(b.id, []).append(a)
    for o in s.operations:
        if o.id not in pos:
            continue
        x, y = pos[o.id]
        label = _man(o.committed_troops) if o.committed_troops > 0 \
            else (o.committed_generals[0] if o.committed_generals else "")   # 개인 이동·복귀 호위=장수명
        marks.append(f'<div class="pc" style="left:{x:.2f}%;top:{y:.2f}%">'
                     f'<span class="dia" style="background:{FCOLOR.get(o.faction, "#888")}"></span>'
                     f'<span class="plb">{label}</span>'
                     f'{_hcard(x, y, _op_hover(s, o, foes.get(o.id, [])))}</div>')
    swords = set()
    for a, b in pairs:                                # 야전·출성 대치: 두 말 사이
        if a.id in pos and b.id in pos:
            (xa, ya), (xb, yb) = pos[a.id], pos[b.id]
            swords.add((round((xa + xb) / 2, 1), round((ya + yb) / 2, 1)))
    for o in s.operations:                            # 공성: 말과 성 사이
        if o.stage == "교전" and o.action.mode == "공성" and o.id in pos \
                and o.action.target in MAP_COORDS:
            (x, y), (cx, cy) = pos[o.id], MAP_COORDS[o.action.target]
            swords.add((round((x + cx) / 2, 1), round((y + cy) / 2, 1)))
    marks += [f'<div class="xw" style="left:{x}%;top:{y}%">⚔</div>' for x, y in swords]

    H(f'<div class="mapwrap"><img src="data:image/jpeg;base64,{img}">{roads}{"".join(marks)}</div>')
    st.caption("성 마커와 부대 말에 마우스를 올리면 상세 정보가 표시됩니다. 마커 색은 현재 소유 세력입니다.")


# ======================= 사건·연혁 2열 (⭐마찰 5: "최근 전황"→주요 사건) =======================
_FACTION_RE = re.compile(r"(?<![가-힣])(위|촉|오)(?=[군가는이의와과,.\s)]|$)")  # 세력명+조사 — "위반·포위" 오탐 방지


def _line_faction(s: GameState, line: str) -> str | None:
    m = _FACTION_RE.search(line)
    if m:
        return m.group(1)
    return next((c.owner for n, c in s.cities.items() if n in line and c.owner in FACTIONS), None)


def events_panel(s: GameState) -> None:
    evs = []
    # ⭐유효 동맹 = 상단 고정 줄(상태라서 「지속」 배지 + 잔여 개월로 일반 사건과 구분)
    for a, b in s.alliances:
        left = s.alliance_expires.get("|".join(sorted((a, b))))
        dots = (f'<span class="who" style="background:{FCOLOR.get(a, "#888")}"></span>'
                f'<span class="who" style="background:{FCOLOR.get(b, "#888")};margin-left:-10px"></span>')
        tail = f' <span style="color:var(--mut)">(잔여 {left}개월)</span>' if left else ""
        evs.append(f'<div class="ev">{dots}동맹 유지 중 — {a}·{b}{tail}<span class="oid">지속</span></div>')
    recent = st.session_state.get("events") or s.history[-8:]
    shown = 0
    for line in recent:
        big = line.startswith("[연혁]")
        if line.startswith("[심판]") or (not big and shown >= 8):
            continue                                  # 심판 사유=결과 창·작전 카드 몫(여긴 사건 헤드라인만)
        f = _line_faction(s, line)
        dot = f'<span class="who" style="background:{FCOLOR.get(f, "#4a5261")}"></span>'
        tag = '<span class="evtag">연혁</span>' if big else ""
        evs.append(f'<div class="ev{" big" if big else ""}">{dot}{_esc(line)}{tag}</div>')
        shown += 1
    chron = "".join(
        f'<div class="chron"><b>{_esc(c.split(": ", 1)[0])}</b> {_esc(c.split(": ", 1)[-1])}</div>'
        for c in reversed(s.chronicle[-8:])) or '<div class="chron">(아직 없음)</div>'
    evs_html = "".join(evs) or '<div class="chron">(아직 없음)</div>'
    H(f'<div class="evcols"><div class="panel"><h3>이번 달 주요 사건</h3>{evs_html}</div>'
      f'<div class="panel"><h3>주요 연혁</h3>{chron}</div></div>')


# ======================= 진행 중 작전 (⭐마찰 17: 경로 바 / 성벽 HP 바 이원화) =======================
def _route_bar(s: GameState, o: ActiveOperation, engaged: bool) -> str:
    n = max(1, round(o.threshold))
    fill = 0.0 if not o.threshold else min(100.0, o.progress / o.threshold * 100)
    nodes = "".join(f'<span class="nd{" on" if i / n * 100 <= fill + 0.1 else ""}" style="left:{i / n * 100:.0f}%"></span>'
                    for i in range(n + 1))
    mk = f'<span class="mk" style="left:{fill:.0f}%;background:{FCOLOR.get(o.faction, "#888")}"></span>'
    cl = f'<span class="cl" style="left:{fill:.0f}%">⚔</span>' if engaged else ""
    return (f'<div class="oprow"><span class="opend">{o.action.origin}</span>'
            f'<div class="route"><div class="ln"><i style="width:{fill:.0f}%"></i></div>{nodes}{mk}{cl}</div>'
            f'<span class="opend">{o.action.target}</span></div>')


def _op_card_html(s: GameState, o: ActiveOperation, foes: list[ActiveOperation]) -> str:
    hold = getattr(o.action, "hold_at", 0)
    status = ("교전 중" if o.stage == "교전"
              else f"{hold}개월 지점 대기 중" if hold and o.progress >= hold
              else f"{o.progress:g}/{o.threshold:g}개월")
    head = (f'<div class="chead" style="margin-bottom:4px"><b style="font-size:13.5px">{_op_name(s, o)}</b>'
            f'{_pill(o.faction, small=True)}{_mpill(_op_kind(o))}<span class="oid">작전 {o.id}</span>'
            f'<div class="grow"></div><span class="fold">{status}</span></div>')
    body = _route_bar(s, o, bool(foes)) if o.action.origin != o.action.target else ""
    cargo = []
    if o.action.mode == "호송":
        cargo = [x for x in (f"장수 {','.join(o.committed_generals)}" if o.committed_generals else "",
                             f"포로 {','.join(o.cargo_prisoners)}" if o.cargo_prisoners else "",
                             f"금 {o.cargo_gold:,}" if o.cargo_gold else "",
                             f"식량 {o.cargo_food:,}" if o.cargo_food else "") if x]
    meta_l = f"병력 {o.committed_troops:,} · 사기 {o.unit_morale}" + (" · " + " · ".join(cargo) if cargo else "")
    meta_r = " · ".join(f"상대 {f.faction} {_op_name(s, f).split(' <')[0]} {f.committed_troops:,}" for f in foes)
    meta = f'<div class="opmeta"><span>{meta_l}</span><span>{meta_r}</span></div>'
    det = ""
    if getattr(o.action, "strategy", ""):
        det = (f'<div class="opdet">전략 <b>「{_esc(o.action.strategy)}」</b>'
               + (f" · 보정 {o.strategy_mod:+.0%}" if o.strategy_mod else "") + "</div>")
    return f'<div class="op">{head}{body}{meta}{det}</div>'


def _siege_card_html(s: GameState, city: str, ops: list[ActiveOperation]) -> str:
    """수성 카드(⭐그룹 문법): 도시 헤더 + 성벽 HP 바 + 공격·출성 부대 = 하위 카드 중첩."""
    c = s.cities[city]
    hp, mx = _wall_hp(c), _wall_max(c)
    head = (f'<div class="chead" style="margin-bottom:4px"><b style="font-size:13.5px">{city}</b>'
            f'{_pill(c.owner, small=True)}{_mpill("수성")}<div class="grow"></div>'
            f'<span class="fold">수비 {c.troops:,}</span></div>')
    bar = (f'<div class="oprow"><span class="opend">성벽</span>'
           f'<div class="optrack"><i style="width:{0 if mx == 0 else hp / mx * 100:.0f}%"></i></div>'
           f'<span class="opend">{hp:,} ∕ {mx:,}</span></div>')
    subs = []
    for o in ops:
        kind = _op_kind(o)
        rows = [_srow("병력", f'{o.committed_troops:,} <span class="k">· 사기 {o.unit_morale}</span>')]
        if getattr(o.action, "strategy", ""):
            rows.append(_srow("전략", f'<span class="vq">「{_esc(o.action.strategy)}」</span>'))
        if o.strategy_mod:
            rows.append(_srow("전략보정", f'<span class="{"pos" if o.strategy_mod > 0 else "neg"}">'
                              f"{o.strategy_mod:+.0%}</span>"))
        if kind == "출성":
            rows.append(_srow("비고", '<span class="k">위협 소멸 시 성내 자동 복귀</span>'))
        subs.append(f'<div class="sub"><div class="chead"><b style="font-size:12.5px">{_op_name(s, o)}</b>'
                    f'{_pill(o.faction, small=True)}{_mpill(kind)}<span class="oid">작전 {o.id}</span></div>'
                    + "".join(rows) + "</div>")
    return f'<div class="op">{head}{bar}{"".join(subs)}</div>'


def _op_buttons(o: ActiveOperation) -> None:
    """내 작전 인라인 회군/전략변경(⭐마찰 발견성) — 중복 큐·상한 초과=비활성."""
    queued = any(getattr(a, "op_id", None) == o.id for a in st.session_state.orders)
    full = orders_left() <= 0
    c1, c2, _ = st.columns([1, 2, 6])
    if c1.button("회군", key=f"op_r{o.id}", disabled=queued,
                 help="철수 명령을 큐에 담습니다(교전 중에는 퇴각 손실). 명령 횟수에 포함되지 않습니다."):
        st.session_state.orders.append(OpCommand(kind="작전지시", op_id=o.id, order="회군"))
        st.rerun()
    if o.action.mode != "호송":                       # 호송=전략 없음(마찰 20)
        box = c2.popover("전략변경") if hasattr(st, "popover") else c2.expander("전략변경")
        with box:
            txt = st.text_input("새 전략(50자)", key=f"op_s{o.id}", max_chars=50)
            if st.button("하달", key=f"op_sb{o.id}", disabled=queued or full or not txt):
                st.session_state.orders.append(
                    OpCommand(kind="작전지시", op_id=o.id, order="전략변경", strategy=txt))
                st.rerun()


def ops_panel(s: GameState) -> None:
    if not s.operations:
        return
    player = st.session_state.player
    H('<div class="phead">진행 중 작전</div>')
    pairs = _field_engagements(s)
    foes: dict[int, list[ActiveOperation]] = {}
    for a, b in pairs:
        foes.setdefault(a.id, []).append(b)
        foes.setdefault(b.id, []).append(a)
    sieged: dict[str, list[ActiveOperation]] = {}
    rest: list[ActiveOperation] = []
    for o in s.operations:
        if o.stage == "교전" and o.action.target in s.cities \
                and (o.action.mode == "공성" or o.action.origin == o.action.target):
            sieged.setdefault(o.action.target, []).append(o)
        else:
            rest.append(o)
    for city, ops in sieged.items():
        if any(o.action.mode == "공성" for o in ops):
            H(_siege_card_html(s, city, ops))
        else:                                         # 출성만 남은 대치 등 — 개별 카드로
            for o in ops:
                H(_op_card_html(s, o, foes.get(o.id, [])))
        for o in ops:
            if player and o.faction == player:
                _op_buttons(o)
    for o in rest:
        H(_op_card_html(s, o, foes.get(o.id, [])))
        if player and o.faction == player:
            _op_buttons(o)


# ======================= 화면: 정세(경보 + 지도 + 사건 + 작전) =======================
def state_panel() -> None:
    s, player = S(), st.session_state.player
    # ⭐군량 경보 + 피침 배너(결정론 술어 — 표에 묻히지 않게)
    if player:
        alerts = [(n, r) for n, c in s.cities.items() if c.owner == player
                  and (r := food_runway(s, n)) is not None and r <= FOOD_ALERT_MONTHS]
        if alerts:
            st.warning("군량 경보 — " + ", ".join(
                f"{n}: " + ("이번 달 고갈 위험" if r == 0 else f"{r}개월 내 고갈") for n, r in alerts))
        threats = [(n, t) for n, c in s.cities.items() if c.owner == player
                   for t in _city_threats(s, n, player)]
        if threats:
            st.error("⚠ 피침 — " + ", ".join(
                f"{n}: {t.faction}군 {t.committed_troops:,}"
                + (" 성 앞 교전 중" if t.stage == "교전" else " 접근 중") for n, t in threats))
    map_panel(s)                                      # ⭐마찰 4: 지도가 핵심 화면(expander 해제)
    events_panel(s)
    ops_panel(s)


# ======================= 도시 표·브리핑 (명령 칸 아래, ⭐마찰 4 배치) =======================
def tables_panel() -> None:
    s, player = S(), st.session_state.player
    _BG = {"위": "background-color: rgba(66,103,178,0.22)",
           "촉": "background-color: rgba(56,158,86,0.22)",
           "오": "background-color: rgba(199,84,80,0.22)"}
    with st.expander("도시 현황 표"):
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
    with st.expander("전체 로그(원문)"):
        st.text("\n".join(s.history[-40:]) or "(아직 없음)")
    if player:
        with st.expander("정세 브리핑(LLM이 보는 그대로)"):
            st.text(brief(s, player))


# ======================= 화면: 명령 작성 =======================
def _order_card(a) -> str:
    """큐 명령 카드(⭐마찰 11 → 목업 qcard 문법): 제목 + mpill + 라벨 행."""
    k = a.kind
    rows: list[tuple[str, str]] = []
    if k == "전투":
        title = "출성" if a.origin == a.target else ("출격" if a.mode == "공성" else a.mode)
        rows = [("경로", f"{a.origin} → {a.target}" if a.origin != a.target else f"{a.origin} 성 앞")]
        if a.generals:
            rows.append(("장수", ", ".join(a.generals)))
        rows.append(("병력", f"{a.troops:,}"))
        if getattr(a, "hold_at", 0):
            rows.append(("대기", f"{a.origin}–{a.target} {a.hold_at}개월 지점"))
        if a.strategy:
            rows.append(("전략", f'<span class="vq">「{_esc(a.strategy)}」</span>'))
    elif k == "내정":
        title = a.item
        rows = [("도시", a.city)]
        if a.general:
            rows.append(("담당", a.general))
        rows.append(("투입", f"금 {a.gold_spent:,}"))
        if a.strategy:
            rows.append(("방침", f'<span class="vq">「{_esc(a.strategy)}」</span>'))
    elif k == "호송":
        title = "호송"
        load = [x for x in (f"병 {a.troops:,}" if a.troops else "", ", ".join(a.generals),
                            ", ".join(f"포로 {p}" for p in a.prisoners),
                            f"금 {a.gold:,}" if a.gold else "", f"식량 {a.food:,}" if a.food else "") if x]
        rows = [("경로", f"{a.origin} → {a.target}"), ("적재", " · ".join(load or ["빈 수레"]))]
        if getattr(a, "round_trip", False):
            rows.append(("왕복", "하역 후 호위 장수만 복귀"))
    elif k == "개인이동":
        title = "개인 이동"
        rows = [("경로", f"{a.origin} → {a.target}"), ("장수", a.general),
                ("비고", "아군 영토 경유 · 전투 면제")]
    elif k == "작전지시":
        title = a.order
        rows = [("대상", f"작전 {a.op_id}")]
        if a.strategy:
            rows.append(("전략", f'<span class="vq">「{_esc(a.strategy)}」</span>'))
    elif k == "외교":
        title = a.proposal
        rows = [("상대", a.target_faction)]
        if a.proposal == "동맹":
            rows.append(("기한", f"{a.months}개월"))
        if getattr(a, "prisoner", ""):
            rows.append(("장수", a.prisoner))
            rows.append(("몸값", f"금 {a.offer_gold:,} · 식량 {a.offer_food:,}"))
        if getattr(a, "message", ""):
            rows.append(("국서", f'<span class="vq">「{_esc(a.message)}」</span>'))
    elif k == "처분":
        title = a.choice
        rows = [("포로", a.prisoner), ("도시", a.city)]
    else:
        title, rows = k, [("내용", _esc(a.model_dump_json(exclude_defaults=True)))]
    body = "".join(_srow(kk, vv) for kk, vv in rows)
    return (f'<div class="qcard"><div class="chead"><b>{title}</b>{_mpill(k)}</div>{body}</div>')


def _clear_inputs(*keys: str) -> None:
    """명령 추가 직후 입력 위젯 초기화(마찰 10: 이전 입력 잔존 방지 — key 삭제=기본값 복귀)."""
    for k in keys:
        st.session_state.pop(k, None)


def order_builder() -> None:
    s, player = S(), st.session_state.player
    left = orders_left()
    used = MAX_ORDERS_PER_TURN - left
    dots = "".join(f'<span class="sdot{"" if i < used else " off"}"></span>'
                   for i in range(MAX_ORDERS_PER_TURN))
    H(f'<div class="phead">명령 ({used}/{MAX_ORDERS_PER_TURN} {dots})</div>')

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
    with st.container(border=True):     # ⭐영역 구분(사용자 2026-09-06): 명령 작성 폼=패널 카드
        # ⭐st.tabs는 rerun마다 첫 탭 리셋(마찰 18) → 상태 유지되는 segmented_control
        tab = st.segmented_control("명령 종류", ["전투", "내정", "호송", "작전지시", "외교"],
                                   key="order_tab", default="전투",
                                   label_visibility="collapsed") or "전투"

        if tab == "전투":                                 # 전투: 출격 UI = LLM 명령서와 같은 양식(§TODO)
            armed = [n for n in mine if s.cities[n].troops > 0]
            if not armed:
                st.caption("출병 가능한 성이 없습니다.")
            else:
                origin = st.selectbox("출발 도시", armed, key="b_o")
                city = s.cities[origin]
                # 이미 큐에 담긴 같은 도시 출격·호송 병력을 차감해 상한 표시(엔진 클램프에 기대지 않기)
                reserved = sum(getattr(o, "troops", 0) for o in st.session_state.orders
                               if getattr(o, "origin", None) == origin and o.kind in ("전투", "호송"))
                avail = max(0, city.troops - reserved)
                modes = ["공성", "야전"] + (["출성"] if _city_threats(s, origin, player) else [])
                mode = st.radio("종류", modes, horizontal=True, key="b_m",
                                help="공성은 인접한 적의 성을 공격하고, 야전은 길 위에서 적 부대와 싸웁니다"
                                     "(적 방면=요격, 아군 방면=구원). 출성은 공격받는 성 앞에 포진합니다.")
                if mode == "출성":
                    target = origin
                else:
                    adj = s.distances.get(origin, {})
                    targets = [n for n in adj if s.cities[n].owner != player
                               and not allied(s, player, s.cities[n].owner)] if mode == "공성" \
                        else [n for n in adj if n != origin]
                    if targets:
                        target = st.selectbox("목표", targets, key="b_t")
                    else:                             # ⭐빈 목표가 항목째 사라져 버그처럼 보이던 것(사용자 2026-09-06)
                        target = None
                        st.caption("이 성에서 공성할 수 있는 인접한 적의 성이 없습니다."
                                   if mode == "공성" else "이 성에서 나갈 수 있는 길이 없습니다.")
                troops = st.number_input(f"병력 (가용 {avail:,})", 1, max(1, avail),
                                         min(10000, max(1, avail)), key="b_n")
                # 장수 1명만(⭐사용자 2026-08-30): 추가 동행은 전투력 0 기여+포획 리스크뿐인 함정 선택지.
                # 장수 재배치는 호송 탭이 전용 동사. AI 경로는 리스트 유지(비대칭 원칙 — 위반 아님).
                gen = st.selectbox("지휘 장수", ["(없음)"] + free(city.generals), key="b_g",
                                   help="통솔이 높은 장수가 부대를 강하게 만듭니다. 지휘 장수가 없는 부대는 "
                                        "전투력이 낮아집니다. 이번 달 다른 임무를 맡은 장수는 목록에서 제외됩니다.")
                # ⭐길목 대기(D묶음): 거리 2+ 야전만 — 지점에 멈춰 길목을 지킨다(요격이 우연→의도)
                hold = 0
                if mode == "야전" and target and target != origin:
                    d = s.distances.get(origin, {}).get(target, 1)
                    if d >= 2:
                        hold = st.selectbox(
                            "길목 대기(선택)", list(range(d)), key="b_h",
                            format_func=lambda n: "끝까지 진군" if n == 0
                            else f"{origin}–{target} {n}개월 지점 대기",
                            help="부대가 해당 지점에 멈춰 길을 지킵니다. 지나는 적은 자동으로 요격되며, "
                                 "교전 후에도 자리를 유지합니다. 해제는 회군(명령 횟수 미포함)입니다.")
                strat = st.text_input("전략(50자)", max_chars=50, key="b_s")
                if st.button("명령 추가", key="b_add", disabled=left <= 0 or target is None or avail <= 0):
                    st.session_state.orders.append(Battle(
                        kind="전투", mode="야전" if mode == "출성" else mode, origin=origin,
                        target=target, troops=int(troops), hold_at=int(hold),
                        generals=[] if gen == "(없음)" else [gen], strategy=strat))
                    _clear_inputs("b_n", "b_g", "b_s", "b_h")
                    st.rerun()

        elif tab == "내정":
            city_n = st.selectbox("도시", mine, key="d_c")
            item = st.radio("항목", ["식량증산", "모병", "사기진작", "성벽보수"], horizontal=True, key="d_i")
            d_avail = max(0, s.cities[city_n].gold - q_gold(city_n))
            gold = st.number_input(f"투입 금 (가용 {d_avail:,})",
                                   0, d_avail, 0, key="d_g",
                                   help="식량증산은 금 1이 식량 2가 됩니다. 모병은 한 번에 많이 투입할수록 효율이 "
                                        "떨어집니다. 성벽보수는 파손된 만큼만(금 3당 1), 사기진작은 80까지 오릅니다.")
            # ⭐담당 장수: 모병=통솔·식량/성벽=지력 비례 효율(안내 한 세트 — 사용자 확정)
            overseer = st.selectbox("담당 장수(선택)", ["(없음)"] + free(s.cities[city_n].generals), key="d_gen",
                                    help="모병은 통솔, 증산·보수는 지력이 높은 담당이 효율을 높입니다. "
                                         "해당 성에 주둔한 장수만 지정할 수 있습니다.")
            strat = ""
            if item == "모병":
                strat = st.text_input("모병 방침(50자 — 심판이 채점합니다)", max_chars=50, key="d_s")
            elif item == "사기진작":
                strat = st.text_input("잔치 한마디(50자, 서사)", max_chars=50, key="d_s2")
            if st.button("명령 추가", key="d_add", disabled=left <= 0):
                st.session_state.orders.append(Domestic(
                    kind="내정", city=city_n, item=item, gold_spent=int(gold),
                    general="" if overseer == "(없음)" else overseer, strategy=strat))
                _clear_inputs("d_g", "d_gen", "d_s", "d_s2")
                st.rerun()

        elif tab == "호송":                               # 호송: 인접 아군 도시만(위젯이 걸러줌)
            tmode = st.radio("종류", ["호송", "개인 이동"], horizontal=True, key="t_mode",
                             help="호송은 병력과 물자를 이웃 아군 성으로 운송합니다(이동 중 요격당할 수 있습니다). "
                                  "개인 이동은 장수 혼자 아군 영토를 거쳐 먼 성까지 안전하게 이동합니다.")
            pairs = [(o, t) for o in mine for t in s.distances.get(o, {}) if t in mine]
            if tmode == "개인 이동":                      # ⭐D묶음 26①: 별도 동사(장수 단독·무사 보장)
                manned = [n for n in mine if free(s.cities[n].generals)]
                if not manned:
                    st.caption("보낼 수 있는 장수가 없습니다.")
                else:
                    origin = st.selectbox("출발", manned, key="v_o")
                    gen = st.selectbox("장수", free(s.cities[origin].generals), key="v_g")
                    dests = [(t, r[1]) for t in mine if t != origin
                             and (r := travel_path(s, player, origin, t))]
                    if not dests:
                        st.caption("아군 영토로 이어진 목적지가 없습니다.")
                    else:
                        lab = dict(dests)
                        target = st.selectbox("목적지", [t for t, _ in dests], key="v_t",
                                              format_func=lambda t: f"{t} (소요 {lab[t]}개월)")
                        if st.button("명령 추가", key="v_add", disabled=left <= 0):
                            st.session_state.orders.append(Travel(
                                kind="개인이동", origin=origin, target=target, general=gen))
                            _clear_inputs("v_g")
                            st.rerun()
            elif not pairs:
                st.caption("호송 가능한 인접 아군 성이 없습니다.")
            else:
                origin = st.selectbox("출발", sorted({o for o, _ in pairs}), key="t_o")
                target = st.selectbox("도착", [t for o, t in pairs if o == origin], key="t_t")
                city = s.cities[origin]
                reserved = sum(getattr(o, "troops", 0) for o in st.session_state.orders
                               if getattr(o, "origin", None) == origin and o.kind in ("전투", "호송"))
                t_avail = max(0, city.troops - reserved)
                troops = st.number_input(f"병사 (가용 {t_avail:,})", 0, t_avail, 0, key="t_n",
                                         help="짐(병사·물자·포로)이 있으면 호위병 200은 붙여야 한다. 장수만 보낼 땐 없어도 된다.")
                gens = st.multiselect("장수", free(city.generals), key="t_g")
                pris = st.multiselect("포로", city.prisoners, key="t_p")
                tg_avail = max(0, city.gold - q_gold(origin))
                tf_avail = max(0, city.food - q_food(origin))
                gold = st.number_input(f"금 (가용 {tg_avail:,})", 0, tg_avail, 0, key="t_gold")
                food = st.number_input(f"식량 (가용 {tf_avail:,})", 0, tf_avail, 0, key="t_f")
                # ⭐왕복 호송(D묶음 26②): 하역 후 호위 장수만 빈 몸으로 복귀 — 요격 노출 2배가 자연 비용
                rt = st.checkbox("왕복 — 도착·하역 후 호위 장수만 복귀", key="t_rt", disabled=not gens,
                                 help="적재물만 내려놓고 호위 장수가 돌아옵니다. 복귀는 개인 이동으로 처리되어 안전합니다.")
                if st.button("명령 추가", key="t_add", disabled=left <= 0):
                    st.session_state.orders.append(Transfer(
                        kind="호송", origin=origin, target=target, troops=int(troops),
                        generals=gens, prisoners=pris, gold=int(gold), food=int(food),
                        round_trip=bool(rt and gens)))
                    _clear_inputs("t_n", "t_g", "t_p", "t_gold", "t_f", "t_rt")
                    st.rerun()

        elif tab == "작전지시":
            ops = [o for o in s.operations if o.faction == player]
            if not ops:
                st.caption("진행 중인 아군 작전이 없습니다.")
            else:
                label = {o.id: f"[{o.id}] {o.action.origin}→{o.action.target} {o.action.mode} ({o.stage})"
                         for o in ops}
                op_id = st.selectbox("작전", list(label), format_func=label.get, key="o_id")
                sel = next(o for o in ops if o.id == op_id)
                # ⭐호송은 전략이 없음(마찰 20): 적게 해놓고 기각하는 함정 제거 — 회군만 노출
                choices = ["회군"] if sel.action.mode == "호송" else ["전략변경", "회군"]
                order = st.radio("지시", choices, horizontal=True, key="o_ord",
                                 help="회군은 명령 횟수에 포함되지 않습니다(교전 중에는 퇴각 손실이 발생합니다). "
                                      "전략변경은 명령 1건을 사용하며 다시 채점됩니다.")
                strat = st.text_input("새 전략(50자)", max_chars=50, key="o_s") if order == "전략변경" else ""
                if st.button("명령 추가", key="o_add", disabled=left <= 0 and order != "회군"):
                    st.session_state.orders.append(OpCommand(
                        kind="작전지시", op_id=op_id, order=order, strategy=strat))
                    _clear_inputs("o_s")
                    st.rerun()

        elif tab == "외교":                               # 외교: 성립 가능한 제안만 노출(파기=동맹 중일 때만, ⭐§9-22)
            others = [f for f in FACTIONS if f != player and s.factions[f].alive]
            if not others:
                st.caption("남은 세력이 없습니다.")
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
                                             help="기한이 만료되면 자연 종료됩니다(배신이 아닙니다). 연장이 수락되면 이 기한으로 재설정됩니다.")
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
                                        help="상대 군주가 국서를 실제로 읽고 판단에 반영합니다.")
                if st.button("명령 추가", key="dp_add", disabled=left <= 0):
                    st.session_state.orders.append(Diplomacy(
                        kind="외교", target_faction=t,
                        proposal="동맹" if prop == "연장" else prop,   # 연장=동맹 재제안(엔진 규약)
                        months=int(months), prisoner=prisoner,
                        offer_gold=int(og), offer_food=int(of), pay_city=pay_city, envoy=envoy, message=msg))
                    _clear_inputs("dp_m", "dp_g", "dp_f")
                    st.rerun()

    # ⭐명령 큐 = 카드(목업 qcard 문법, 마찰 11) + 취소 버튼
    if st.session_state.orders:
        H('<div class="phead">명령 큐</div>')
        cols = st.columns(2)
        for i, a in enumerate(st.session_state.orders):
            with cols[i % 2]:
                H(_order_card(a))
                if st.button("✕ 취소", key=f"del{i}"):
                    st.session_state.orders.pop(i)
                    st.rerun()

    # 포로 담화(설득)·후속 처분 — 담화=슬롯 즉시 소모(§9-21), 석방/처형=처분 명령(⭐배치4)
    held = [(c.name, p) for c in s.cities.values() if c.owner == player for p in c.prisoners]
    if held:
        st.markdown("**수감 중인 포로**")
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


# ======================= 턴 종료 고정 바 (⭐버튼 ↔ 판정 진행 스텝 같은 자리 전환) =======================
def endturn_bar() -> None:
    s, player = S(), st.session_state.player
    left = orders_left()
    used = MAX_ORDERS_PER_TURN - left
    slot = st.empty()
    with slot.container():
        c1, c2 = st.columns([2, 3])
        if player:
            c1.caption(f"명령 {used}/{MAX_ORDERS_PER_TURN} · 미사용 {left}")
        else:
            c1.caption("관전 — AI 자율 진행")
        if c2.button(f"턴 종료 — {s.month}월 판정", type="primary", use_container_width=True):
            end_turn(slot)
            st.rerun()


# ======================= 화면: 담화 =======================
def parley_screen() -> None:
    s = S()
    ctx = st.session_state.parley
    city, prisoner = ctx["city"], ctx["prisoner"]
    st.subheader(f"담화 — {prisoner} ({city} 수감)")
    st.caption(f"말은 {PARLEY_MAX_ROUNDS}마디까지 건넬 수 있습니다. 담화를 마치면 심판이 채점해 설득을 판정합니다.")
    for who, line in ctx["transcript"]:
        st.chat_message("user" if who == "군주" else "assistant").write(f"**{who}**: {line}")

    if ctx["verdict"] is None:
        rounds = sum(1 for w, _ in ctx["transcript"] if w == "군주")
        if rounds < PARLEY_MAX_ROUNDS:
            if line := st.chat_input("설득의 말..."):
                ctx["transcript"].append(("군주", line.strip()))
                with st.spinner(f"{prisoner}이(가) 입을 엽니다..."):
                    reply = prisoner_reply(s, city, prisoner, ctx["transcript"])
                ctx["transcript"].append((prisoner, reply))
                st.rerun()
        c1, c2 = st.columns(2)
        if c1.button("담화 종료(심판 채점)", disabled=not ctx["transcript"], type="primary"):
            with st.spinner("심판이 담화를 채점하고 있습니다..."):
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
        (st.success if ok else st.warning)("귀순!" if ok else "설득에 실패했습니다 — 포로가 마음을 닫았습니다.")
        if st.button("돌아가기"):
            st.session_state.mode = "play"
            st.rerun()


# ======================= 화면: 턴 결과 창 (⭐마찰 12: 카드 그룹핑) =======================
_OPID_RE = re.compile(r"작전(\d+)")
_JUDGE_RE = re.compile(r"(\d+)/10.*?([+\-−±]\d+)%")


def _result_groups(s: GameState, events: list[str]):
    """이벤트 로그 → (연혁 배너, 세력별 카드, 기타). 카드=같은 작전번호 줄 묶음([심판] 포함)."""
    banners: list[str] = []
    op_cards: dict[str, dict] = {}                    # opid → {faction, lines}
    loose: dict[str, list[str]] = {}                  # faction → 단독 줄
    misc: list[str] = []
    last: dict | None = None
    for e in events:
        if e.startswith("[연혁]"):
            banners.append(e[len("[연혁]"):].strip())
            last = None
            continue
        if e[:1].isspace() and last is not None:      # "  ↳ ..." 연쇄 줄은 직전 카드에
            last["lines"].append(e.strip())
            continue
        fac = _line_faction(s, e)
        m = _OPID_RE.search(e)
        if m:
            card = op_cards.setdefault(m.group(1), {"faction": fac, "lines": []})
            card["faction"] = card["faction"] or fac
            card["lines"].append(e)
            last = card
        elif fac:
            loose.setdefault(fac, []).append(e)
            last = None
        else:
            misc.append(e)
            last = None
    return banners, op_cards, loose, misc


def _render_rescard(lines: list[str], color: str) -> str:
    judge = next((l for l in lines if l.startswith("[심판]")), None)
    chip = ""
    if judge:
        m = _JUDGE_RE.search(judge)
        if m:
            cls = " zero" if m.group(2).lstrip("+-−±") == "0" else \
                " neg" if m.group(2)[0] in "-−" else ""
            chip = f'<span class="jchip{cls}">심판 {m.group(1)}/10 → {m.group(2)}%</span>'
    head_line = next((l for l in lines if not l.startswith("[심판]")), lines[0])
    rest = [l for l in lines if l is not head_line and not l.startswith("[심판]")]
    body = "".join(f'<div class="rline">{_esc(l)}</div>' for l in rest)
    jwhy = f'<div class="jwhy">{_esc(judge)}</div>' if judge else ""
    return (f'<div class="rescard" style="border-left-color:{color}">'
            f'<div class="chead"><b style="font-size:13.5px">{_esc(head_line)}</b>{chip}</div>'
            f"{body}{jwhy}</div>")


def results_screen() -> None:
    s, player = S(), st.session_state.player
    st.subheader(f"{s.year}년 {s.month}월 — 지난 달 결과")
    if st.session_state.narrate and st.session_state.events:
        if st.session_state.narration is None:
            with st.spinner("사관이 붓을 들었습니다..."):
                try:
                    st.session_state.narration = structured_complete(
                        Narration, load_prompt("narration"),
                        "\n".join(st.session_state.events)).text
                except LLMError:
                    st.session_state.narration = "(이 달의 기록은 소실되었다)"
        st.markdown(f"> {st.session_state.narration}")

    banners, op_cards, loose, misc = _result_groups(s, st.session_state.events)
    for b in banners:                                 # ⭐대사건=배너(연혁급 강조)
        H(f'<div class="banner">{_esc(b)}</div>')
    order = ([player] if player else []) + [f for f in FACTIONS if f != player]
    for f in order:
        cards = [c for c in op_cards.values() if c["faction"] == f]
        lines = loose.get(f, [])
        if not cards and not lines:
            continue
        tag = " (나)" if f == player else ""
        H(f'<div class="fsec"><div class="chead">{_pill(f, f + tag)}</div></div>')
        for c in cards:
            H(_render_rescard(c["lines"], FCOLOR.get(f, "#666")))
        if lines:
            H(_render_rescard(lines, FCOLOR.get(f, "#666")) if len(lines) == 1 else
              '<div class="rescard" style="border-left-color:%s">%s</div>'
              % (FCOLOR.get(f, "#666"),
                 "".join(f'<div class="rline">{_esc(l)}</div>' for l in lines)))
    stray = [c for c in op_cards.values() if c["faction"] not in FACTIONS]
    if misc or stray:
        H('<div class="fsec"><div class="chead"><span class="mpill">그 외</span></div></div>')
        for c in stray:
            H(_render_rescard(c["lines"], "#444c5c"))
        if misc:
            H('<div class="rescard">%s</div>'
              % "".join(f'<div class="rline">{_esc(l)}</div>' for l in misc))
    if not st.session_state.events:
        st.text("(변화 없음)")
    if st.session_state.eco:                          # ⭐경제 요약 줄(목업 ecoline)
        dg, df_, secs = st.session_state.eco
        alerts = [f"⚠ {n} 군량 {r}개월" for n, c in s.cities.items() if c.owner == player
                  and (r := food_runway(s, n)) is not None and r <= FOOD_ALERT_MONTHS]
        H('<div class="ecoline">'
          + f"<span>금 {dg:+,}</span><span>군량 {df_:+,}</span>"
          + "".join(f'<span class="neg">{a}</span>' for a in alerts)
          + f'<span class="grow"></span><span>턴 소요 {secs:.0f}s</span></div>')
    with st.expander("전체 로그(원문)"):
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
        H('<div class="phead">포로 처분</div>')
        st.caption("새로 사로잡은 포로입니다. 설득은 다음 달 담화에서 할 수 있습니다.")
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
    if my_props:                                      # ⭐영역 구분(사용자 2026-09-06): 서브타이틀 + 패널 카드
        H('<div class="phead">외교 제안</div>')
    for i, prop in enumerate(my_props):
        detail = {"동맹": "동맹 제안", "항복권유": "항복 권유 — 수락하면 나라를 넘기고 패배합니다",
                  "포로반환": f"포로 {prop.prisoner} 반환 요청(몸값 금{prop.offer_gold}·식{prop.offer_food})"
                  }[prop.proposal]
        with st.container(border=True):
            st.markdown(_pill(prop.from_faction, small=True)
                        + f" **{prop.from_faction}의 {detail}**"
                        + (f" · 사신 {prop.envoy}" if prop.envoy else ""), unsafe_allow_html=True)
            if prop.message:
                st.markdown(f"> {prop.message}")
            say = st.text_input("답신 한마디(선택 — 상대가 읽고 응수합니다)", max_chars=50,
                                key=f"say_{prop.from_faction}_{prop.proposal}_{prop.prisoner}")  # 인덱스 키=텍스트 상속 버그
            c1, c2 = st.columns(2)
            for col, accept in ((c1, True), (c2, False)):
                if col.button("수락" if accept else "거절", key=f"resp{i}_{accept}",
                              use_container_width=True):
                    respond_proposal(s, prop, accept, say)
                    if say:                           # 옵트인 응수: 판정 이후, 결과 영향 0
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

st.markdown(_CSS, unsafe_allow_html=True)

with st.sidebar:
    st.title("삼국지 LLM")
    if st.session_state.mode != "setup":
        who = st.session_state.player or "관전"
        st.caption(f"모드: {who} · 시드 {S().seed}")
        # ⭐마찰 13: 서사 토글을 게임 중에도(사이드바) — key 바인딩으로 세션 상태와 동기
        st.toggle("소설풍 턴 서사", key="narrate", help="매달 사관이 소설풍 기록을 남깁니다. 다음 결과 창부터 반영됩니다.")
        if st.button("📖 룰북"):
            rulebook()
        if st.session_state.mode == "play" and not st.session_state.over:
            if st.button("💾 저장"):                   # 턴 경계에서만(작성 중 명령 등 휘발 상태 없음)
                st.toast(f"저장됨: {save_game().stem}")
        if st.button("🔄 처음부터"):
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
    topbar()
    state_panel()
    if st.session_state.player:
        order_builder()
    else:
        st.caption("관전 모드 — 매달 세 세력의 AI가 자율적으로 결정합니다.")
    tables_panel()
    if not st.session_state.over:
        endturn_bar()
    if st.session_state.over:                         # 관전 종료 등
        st.header(st.session_state.over)
