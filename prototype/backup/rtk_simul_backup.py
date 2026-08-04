import streamlit as st
import operator
import json
import io
import pandas as pd
from contextlib import redirect_stdout
from typing import Annotated, List, TypedDict
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, START, END

load_dotenv()

st.set_page_config(layout="wide")

class GameState(TypedDict):
    year: int
    month: int
    kingdoms: dict
    actions: Annotated[dict, operator.ior]
    history: List[str]

llm = init_chat_model('openai:gpt-4.1-mini', temperature=0.7)

def kingdom(state: GameState, kingdom_name: str):
    recent_six_month = '\n'.join(state['history'][-6:]) 
    
    system_prompt = f'''
    당신은 삼국지 {kingdom_name}나라의 군주입니다.
    목표는 천하통일입니다. 현재 상황을 보고 이번 달에 취할 하나의 행동을 결정하세요.
    행동의 종류는 다음과 같습니다 : 전투(도시 함락) / 외교(친선, 동맹 등) / 계략(내통자 포섭 등) / 내정

    현재 상황: {state['kingdoms'][kingdom_name]}
    과거 역사: {recent_six_month}

    **어떠한 부연 설명도 하지 말고, 반드시 아래의 JSON 형식으로만 답변을 출력하세요.**
    {{
        "행동_종류": "[전투, 외교, 계략, 내정 중 가장 적절한 것 1개 선택]",
        "상세_행동": "[현재 상황과 역사를 반영하여 군주로서 내리는 구체적인 지시를 1문장으로 작성]"
    }}
    '''
    
    response = llm.invoke([{'role': 'system', 'content': system_prompt}, 
                           {'role': 'user', 'content': '이번 달 우리의 전략은 무엇입니까?'}])
    
    result_data = json.loads(response.content)
    action_text = result_data.get("상세_행동", "")

    print(f'{kingdom_name}나라의 행동 : {action_text}')
    
    return {'actions': {kingdom_name: action_text}}

def judge(state: GameState):
    actions = state['actions']
    kingdoms = state['kingdoms']
    
    system_prompt = f'''
    당신은 삼국지의 심판입니다. 아래 세 나라의 행동을 판정하세요.
    
    [현재 세력 상태]
    {json.dumps(kingdoms, ensure_ascii=False)}
    
    [각 나라의 행동]
    {json.dumps(actions, ensure_ascii=False)}
    
    **결과는 반드시 아래 JSON 형식으로만 답변하세요:**
    {{
        "description": "세 세력이 행동한 결과에 대한 상세한 역사적 묘사 (한 문장)",
        "updated_kingdoms":{{ 
            "위": {{ 보유_도시 등 수정된 위나라 전체 데이터 }},
            "촉": {{ 보유_도시 등 수정된 촉나라 전체 데이터 }},
            "오": {{ 보유_도시 등 수정된 오나라 전체 데이터 }}
    }}
    }}

    **주의: 한 세력이 획득한 도시는 다른 세력으로부터 탈취한 도시여야 합니다.**
    '''

    response = llm.invoke(system_prompt)
    
    result_data = json.loads(response.content)
    result_text = result_data['description']
    updated_kingdoms = result_data['updated_kingdoms']

    new_history = state['history'] + [f"[{state['year']}년 {state['month']}개월] {result_text}"]
    print(f"[{state['year']}년 {state['month']}개월] {result_text}")

    new_month = state['month'] + 1
    new_year = state['year']
    if new_month > 12:
        new_month = 1
        new_year += 1
    
    return {
        'year': new_year,
        'month': new_month,
        'kingdoms' : updated_kingdoms,
        'history': new_history,
        'actions': {}, 
    }

def check_limit(state: GameState):
    return END

workflow = StateGraph(GameState)

workflow.add_node("위", lambda state: kingdom(state, "위"))
workflow.add_node("촉", lambda state: kingdom(state, "촉"))
workflow.add_node("오", lambda state: kingdom(state, "오"))
workflow.add_node("심판", judge)

workflow.add_edge(START, "위")
workflow.add_edge("위", "촉")
workflow.add_edge("촉", "오")
workflow.add_edge("오", "심판")
workflow.add_conditional_edges("심판", check_limit)

graph = workflow.compile()

if 'state' not in st.session_state:
    try:
        with open('../data/three_kingdoms_status.json', 'r', encoding='utf-8') as f:
            kingdoms_default = json.load(f)
    except FileNotFoundError:
        kingdoms_default = {"위": {}, "촉": {}, "오": {}}

    st.session_state.state = {
        "year": 0,
        "month": 1,
        "kingdoms": kingdoms_default,
        "actions": {},
        "history": []
    }

if 'output' not in st.session_state:
    st.session_state.output = ""


col1, col2 = st.columns([2, 1])

with col1:
    st.header(f"[{st.session_state.state['year']}년 {st.session_state.state['month']}개월] 시뮬레이션")
    
    if st.button("다음 달 시뮬레이션 진행"):
        f = io.StringIO()
        with redirect_stdout(f):
            st.session_state.state = graph.invoke(st.session_state.state)
        st.session_state.output = f.getvalue()
        st.rerun()

    if st.session_state.output:
        st.text_area("이번 달 결과", value=st.session_state.output, height=200, disabled=True)

with col2:
    st.header("세력 상태")
    for faction, status in st.session_state.state['kingdoms'].items():
        st.subheader(faction)

        safe_status = {}
        for k, v in status.items():
            if isinstance(v, list):
                safe_status[k] = ", ".join(map(str, v))
            else:
                safe_status[k] = str(v)

        st.table(pd.DataFrame([safe_status]).T)