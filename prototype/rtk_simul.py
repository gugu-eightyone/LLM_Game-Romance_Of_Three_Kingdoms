import streamlit as st
import operator
import json
from typing import Annotated, List, TypedDict
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, START, END
from dotenv import load_dotenv

load_dotenv()

class GameState(TypedDict):
    year: int
    month: int
    kingdoms: dict
    actions: Annotated[dict, operator.ior]
    processed_actions: dict
    history: List[str]

llm = init_chat_model('openai:gpt-4.1-mini', temperature=0.7)

def kingdom(state: GameState, kingdom_name: str):
    recent_six_month = '\n'.join(state['history'][-6:])
    kingdoms = state['kingdoms']

    system_prompt = f'''
    당신은 삼국지 {kingdom_name}나라의 군주입니다.
    목표는 천하통일입니다. 현재 상황을 보고 이번 달에 취할 하나의 행동을 결정하세요.
    행동의 종류는 다음과 같습니다 : 전투(도시 공격 및 수성, 퇴각 등) / 외교(친선, 동맹 등) / 계략(밀정 투입, 방어 시설 붕괴 등) / 내정

    현재 상황: {json.dumps(kingdoms[kingdom_name], ensure_ascii=False)}
    과거 역사: {recent_six_month}

    **어떠한 부연 설명도 하지 말고, 반드시 아래의 JSON 형식으로만 답변을 출력하세요.**
    {{
        "action_details": "[현재 상황을 반영하여 군주로서 내리는 구체적인 지시를 하나의 문장으로 작성]"
    }}
    '''

    response = llm.invoke([{'role': 'system', 'content': system_prompt},
                           {'role': 'user', 'content': '이번 달 우리의 전략은 무엇입니까?'}])

    result_data = json.loads(response.content)
    action_text = result_data.get("action_details", "")

    return {'actions': {kingdom_name: action_text}}

def player_kingdom(state: GameState, kingdom_name: str, action_text: str):
    return {'actions': {kingdom_name: action_text}}

def judge(state: GameState):
    actions = state['actions']
    kingdoms = state['kingdoms']
    processed_actions = actions

    system_prompt = f'''
    당신은 삼국지의 심판입니다. 아래 세 나라의 행동에 대해 결과를 판단하세요.

    [전투 및 사건 판정 기준]
    1. 자원의 증감: 전투를 치르면 병사와 금_곡식이 감소하고, 내정을 하면 증가합니다.
    2. 자원 우위:'금_곡식'의 상태는 보급과 직결됩니다. 보급이 부족한 세력은 전투에서 승리하기 매우 어렵습니다.
    3. 장수 변수:자원이 다소 부족하더라도 압도적으로 강력한 장수가 있다면 전황을 뒤집을 수 있으며, 적장을 포획(general_capture)할 확률이 높아집니다.
    4. 수성의 이점: 수성이 공성보다 유리합니다. 다만 투석기나 충차를 동원하면, 도시 함락(city_capture)할 확률이 높아집니다.
    5. 전략적 상성: 세 나라의 '행동'을 종합적으로 분석하세요. 한 세력이 두 세력으로부터 동시에 공격받거나, 공격을 나간 사이 본진을 기습당하는 경우 방어력이 급격히 하락한 것으로 간주하세요.

    [현재 세력 상태]
    {json.dumps(kingdoms, ensure_ascii=False)}

    [각 나라의 행동]
    {json.dumps(actions, ensure_ascii=False)}

    **결과는 반드시 아래 JSON 형식으로만 답변하세요:**
    {{
        "description": "세 세력이 행동한 결과에 대한 상세한 역사적 묘사 (한 문장)",
        "events": [
        {{
            "type": "city_capture",
            "from": "도시를 빼앗긴 세력 (위, 촉, 오)",
            "to": "도시를 얻은 세력 (위, 촉, 오)",
            "target_city": "대상 도시명"
        }},
        {{
            "type": "general_capture",
            "from": "장수가 원래 소속된 세력 (위, 촉, 오)",
            "to": "장수를 포획한 세력 (위, 촉, 오)",
            "target_general": "포로가 된 장수 이름"
        }},
        {{
            "type": "military_change",
            "kingdom": "세력명 (위, 촉, 오)",
            "status": "다음 중 하나 선택: 매우 많음, 많음, 보통, 적음, 매우 적음"
        }},
        {{
            "type": "resource_change",
            "kingdom": "세력명 (위, 촉, 오)",
            "status": "다음 중 하나 선택: 매우 풍족, 풍족, 보통, 부족, 매우 부족"
        }}
        ]
    }}
    '''

    response = llm.invoke(system_prompt)
    result_data = json.loads(response.content)
    result_text = result_data['description']
    events = result_data.get('events', [])

    for event in events:
        event_type = event.get('type')
        if event_type == 'city_capture':
            loser, winner, city = event['from'], event['to'], event['target_city']
            if city in kingdoms[loser]['보유_도시']:
                kingdoms[loser]['보유_도시'].remove(city)
                if city not in kingdoms[winner]['보유_도시']:
                    kingdoms[winner]['보유_도시'].append(city)

        if event_type == 'general_capture':
            loser, winner, general = event['from'], event['to'], event['target_general']
            if general in kingdoms[loser]['휘하_장수']:
                kingdoms[loser]['휘하_장수'].remove(general)
                if general not in kingdoms[winner]['포로']:
                    kingdoms[winner]['포로'].append(general)

        if event_type == 'military_change':
            kingdoms[event['kingdom']]['병사'] = event['status']

        if event_type == 'resource_change':
            kingdoms[event['kingdom']]['금_곡식'] = event['status']

    new_history = state['history'] + [f"[{state['year']}년 {state['month']}월] {result_text}"]

    new_month = state['month'] + 1
    new_year = state['year']
    if new_month > 12:
        new_month = 1
        new_year += 1

    return {
        'year': new_year,
        'month': new_month,
        'kingdoms': kingdoms,
        'history': new_history,
        'actions': {},
        'processed_actions': processed_actions
    }

def check_limit(state: GameState):
    return END

st.set_page_config(layout="wide")

if "game_state" not in st.session_state:
    st.session_state.game_state = {
        "year": 0,
        "month": 1,
        "kingdoms": {
            "위": {
                "보유_도시": ["낙양", "업", "장안", "허창", "진류", "양양", "합비"],
                "휘하_장수": ["조조", "사마의", "순욱", "곽가", "가후", "하후돈", "하후연", "장료", "서황", "장합", "조인", "허저"],
                "포로": [],
                "병사": "매우 많음",
                "금_곡식": "풍족"
            },
            "촉": {
                "보유_도시": ["성도", "한중", "강주", "자동"],
                "휘하_장수": ["유비", "제갈량", "관우", "장비", "조운", "마초"],
                "포로": [],
                "병사": "보통",
                "금_곡식": "부족"
            },
            "오": {
                "보유_도시": ["건업", "오창", "시상", "회계"],
                "휘하_장수": ["손권", "주유", "노숙", "육손", "여몽", "감녕"],
                "포로": [],
                "병사": "많음",
                "금_곡식": "매우_풍족"
            }
        },
        "actions": {},
        "processed_actions": {},
        "history": []
    }
    st.session_state.user_choice = None

if st.session_state.user_choice is None:
    st.title("🚩 삼국지 전략 시뮬레이션")
    selected_kingdom = st.selectbox("플레이할 세력을 선택하세요", ["위", "촉", "오"])
    if st.button("시작하기"):
        st.session_state.user_choice = selected_kingdom
        st.rerun()
else:
    user_choice = st.session_state.user_choice
    state = st.session_state.game_state

    # 게임 종료 화면
    if st.session_state.get('game_over', False):
        st.title("🚩 삼국지 전략 시뮬레이션")
        st.subheader("⏰ 1년이 지났습니다. 시뮬레이션 종료!")
        kingdoms = state['kingdoms']
        st.write("### 최종 세력 현황")
        for name in ['위', '촉', '오']:
            st.write(f"**{name}** | 도시: {len(kingdoms[name]['보유_도시'])}개 ({', '.join(kingdoms[name]['보유_도시'])}) | 병사: {kingdoms[name]['병사']} | 자원: {kingdoms[name]['금_곡식']}")
        st.write("### 전체 역사")
        for log in state['history']:
            st.info(log)
        if st.button("처음으로"):
            del st.session_state.game_state
            del st.session_state.user_choice
            del st.session_state.game_over
            st.rerun()
        st.stop()

    st.title("🚩 삼국지 전략 시뮬레이션")
    st.subheader(f"🗓️ {state['year']}년 {state['month']}월 상황")

    col1, col2, col3, col4 = st.columns([2.5, 2.5, 2.5, 2.5])

    kingdoms = state['kingdoms']
    for idx, (name, col) in enumerate(zip(['위', '촉', '오'], [col1, col2, col3])):
        with col:
            st.header(name)
            st.write(f"🏰 도시: {', '.join(kingdoms[name]['보유_도시'])}")
            st.write(f"⚔️ 병사: {kingdoms[name]['병사']} | 💰 자원: {kingdoms[name]['금_곡식']}")
            st.write(f"👥 장수: {', '.join(kingdoms[name]['휘하_장수'])}")
            st.write(f"⛓️ 포로: {', '.join(kingdoms[name]['포로']) if kingdoms[name]['포로'] else '없음'}")

    with col4:
        st.header("6개월 히스토리")
        recent_history = state['history'][-6:]
        if recent_history:
            for log in reversed(recent_history):
                st.info(log)
        else:
            st.write("평화로운 시대입니다.")

    st.divider()

    action_col_left, action_col_right_input, action_col_right_btn = st.columns([5, 4, 1])

    with action_col_left:
        st.subheader("금월 국가별 행동 및 역사")
        last_actions = state.get('processed_actions', {})
        if last_actions:
            st.write(f"**위:** {last_actions.get('위', '-')}")
            st.write(f"**촉:** {last_actions.get('촉', '-')}")
            st.write(f"**오:** {last_actions.get('오', '-')}")
            if state['history']:
                st.success(f"📜 결과: {state['history'][-1].split(']', 1)[-1]}")
        else:
            st.write("첫 턴을 시작해 주세요.")

    with action_col_right_input:
        player_input = st.text_input(f"{user_choice}나라의 행동 입력:", key="action_input")

    with action_col_right_btn:
        st.write(" ")
        st.write(" ")
        if st.button("ENTER"):
            if player_input:
                _captured_input = player_input

                builder = StateGraph(GameState)
                builder.add_node('위', lambda x: player_kingdom(x, '위', _captured_input) if user_choice == '위' else kingdom(x, '위'))
                builder.add_node('촉', lambda x: player_kingdom(x, '촉', _captured_input) if user_choice == '촉' else kingdom(x, '촉'))
                builder.add_node('오', lambda x: player_kingdom(x, '오', _captured_input) if user_choice == '오' else kingdom(x, '오'))
                builder.add_node("심판", judge)

                builder.add_edge(START, '위')
                builder.add_edge(START, '촉')
                builder.add_edge(START, '오')
                builder.add_edge('위', '심판')
                builder.add_edge('촉', '심판')
                builder.add_edge('오', '심판')
                builder.add_conditional_edges('심판', check_limit)
                graph = builder.compile()

                new_state = graph.invoke(state)
                st.session_state.game_state = new_state

                # 1년(12개월) 경과 시 게임 종료
                if new_state['year'] >= 1:
                    st.session_state.game_over = True

                st.rerun()
            else:
                st.warning("행동을 입력하세요.")