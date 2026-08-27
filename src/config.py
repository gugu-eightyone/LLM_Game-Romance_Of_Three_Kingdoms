"""게임 밸런스·캘리브레이션 상수 — 로직과 분리한 튜닝 손잡이 한 곳(플레이테스트로 조정).

로직(models·engine)을 import하지 않는다(순환 방지). 여긴 순수 상수만.
설계 근거·트레이드오프는 docs/DISCUSSION.md 참조.
"""

# --- 전략 서술(LLM) ---
STRATEGY_MAX_CHARS = 50          # 전략 자유서술 상한. 담화 아닌 지시라 짧게. [[DISCUSSION#9-8]]
STRATEGY_MODIFIER_BOUND = 0.15   # judge 전략점수 → 전투 보정 상한(±). 결정론 코어가 지배하도록. (judge 미배선)

# --- 이동/지형 ---
FACTION_SPEED = {"오": 1.25}     # 진군 진행도/월(세력별). 미지정=DEFAULT_SPEED. 오=장강 고속. [[DISCUSSION#9-9]]
DEFAULT_SPEED = 1.0              # 기본 진군 속도. 이동 threshold=거리(개월).
RIVER_CROSS_PENALTY = 1          # 강 구간 도하 지연(개월). 위·촉만(오는 수전 면제).
FACTION_NAVAL = {"오": 2, "위": 1, "촉": 1}  # 증분2 야전 수전 보정용 예약(이동 미사용).

# --- prep(조기도착 선취) ---
PREP_RATE = 0.2                  # 조기도착 잉여(개월) → 첫 교전 우세도 보정 환산.
PREP_CAP = 0.10                  # prep 보정 상한(결정론 코어 지배 유지, MODIFIER_BOUND보다 작게).

# --- 전투 ---
GENERAL_SCALE = 500              # 통솔 보정 스케일: 병력배수 = 1 + 최고통솔/SCALE
WALL_DEFENSE = 2500              # 성벽 레벨당 수비 병력환산 보너스(수성 우위 손잡이).
ATTRITION_RATE = 0.10            # 교전 월 손실 계수(상대 전투력 대비).
SIEGE_RATE = 3                   # 교전 우세도(전력비-1) → 진행도/월.
SIEGE_BASE = 2                   # 교전 threshold = SIEGE_BASE + 성벽레벨.

# --- 내정 ---
DOMESTIC_GAIN = 2                # 금 1 지출 → 식량/병력 2 (모병·식량증산).

# --- 호송/명령 상한 (2026-08-27) ---
ESCORT_MIN_TROOPS = 200          # 병사·물자·포로 호송 최소 호위 병력. 장수 단독 이동은 면제.
MAX_ORDERS_PER_TURN = 4          # 세력당 턴 명령 상한(LLM 폭주 안전핀). 초과분 잘라내고 [환각] 로깅(A층 표면).

# --- 포로화 ---
CAPTURE_FLOOR = 0.2              # 포획확률 최솟값(가산 아님). 포획확률 = max(FLOOR, 포위도²). [[DISCUSSION#9-10]]

# --- 사기(전투력 배수) ---
MORALE_COMBAT_BAND = 0.3        # 사기 전투력 배수 폭: 1 + (morale−50)/50 × BAND (m0→0.7·m50→1.0·m100→1.3). 통솔의 ~2배, 스노볼 억제. [[DISCUSSION#9-10]]

# --- 야전(증분2: 지속 전투·요격·구원군·마일스톤) ---
UNIT_MORALE_COMBAT_DROP = 3     # 교전 1회당 부대 사기 감소(전투 소모). 보급차단 대량감소·재연결 회복은 추후.
# 협공 보너스는 결정론 상수로 두지 않음 — "좋은 협공인가"는 배치 따라 뒤집히는 판단이라 judge 넛지 몫.
# 협공 효과 자체는 "두 번 맞는다"(수성 라운드+구원군 라운드)로 공짜 발생. [[DISCUSSION#9-10]]
ROUT_MORALE_THRESHOLD = 30      # 이 사기 미만이면 확률적 강제 퇴각 발동. 확률=((T−m)/T)²(아래로 볼록, m=0→1). seeded RNG.
FIELD_RETREAT_LOSS = 0.2        # 강제 퇴각 시 추가 병력 손실(급히 빠지는 대가). judge가 경감 가능(나중).
FIELD_CAPTURE_BASE = 0.15       # 야전 전멸 시 장수 포획 기본확률 × 그 턴 교전 적 부대 수. 최근접 적 도시 호송(탈영 없음). [[DISCUSSION#9-10]]
