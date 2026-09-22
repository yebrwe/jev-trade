# jev_trade

Binance USDT-M 선물 자동매매 시스템. 데이터 수집·지표 계산·리스크·주문은 **코드**가 소유하고,
TypeSafe의 System One 모델 **Jev**는 지표 요약을 읽고 `long / short / hold / exit` 판단에 필요한
좁은 질문에만 답합니다.

```
Binance ──► 캔들(10개 TF) + 실시간(티커·펀딩·호가) + 내 포지션
              │
              ▼  indicators.py (EMA/RSI/MACD/ADX/BB/ATR/Stoch/거래량/스윙)
              ▼  semantics.py  (숫자 → "strong uptrend", "overbought (74), falling" 같은 의미 버킷)
              ▼  state.py      (Jev에 보낼 컴팩트 JSON state, ~4.5k 토큰)
              ▼  judge.py      (한 번의 요청에 Choice/Score/Noul 질문을 병렬로)
              ▼  policy.py     (confidence 게이트, 포지션 인지, 쿨다운, ATR 기반 사이징 → 최종 행동)
              ▼  exchange.py   (PaperBroker 또는 LiveBroker: 시장가 진입 + 거래소 SL/TP 예약)
              ▼  logs/decisions.jsonl (state·답변·결정·체결 전부 기록 → 임계값 튜닝용)
```

## 왜 이런 구조인가

TypeSafe 문서의 [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)에 따르면
Jev는 **숫자 계산·카운팅·날짜 비교에 약하고, 문자 그대로 읽으며, 상태에 잡음이 많으면 정확도가 떨어집니다.**
그래서:

- 원시 캔들이나 지표 숫자 배열은 절대 보내지 않습니다. 모든 비교(EMA 정렬, RSI 구간, ATR 백분위 등)는
  코드에서 끝내고 결과를 문장으로 보냅니다.
- 질문은 원자적으로 쪼갭니다. `entry_action`(Choice), `setup_quality`(Score),
  `higher_lower_agree` / `choppy` / `overextended`(Noul), 포지션이 있을 때만 `position_action`(Choice)과
  `thesis_invalidated`(Noul). 모두 **한 요청**에 담겨 병렬 평가됩니다.
- Jev의 답은 코드가 합성합니다. 진입은 confidence·확률·setup·choppy 게이트를 모두 통과해야 하고,
  청산은 `position_action=exit`, `thesis_invalidated`, 반대 방향 진입 신호 중 하나면 됩니다.
  손절/익절은 Jev와 무관하게 ATR 배수로 거래소에 예약됩니다.

## 관점 레이어: Claude가 쓰고 Jev가 읽는 거시·당일 뷰

지표만으로는 미국 정부 발표, FOMC, 해킹·전쟁 같은 사건을 알 수 없습니다. 그래서 두 모델을 역할별로 나눕니다.

```
하루 1회 (또는 24h 경과 / UTC 날짜 변경)          매 NEWS_CHECK_MINUTES (기본 10분) + 1분봉 급변동 시 즉시
┌──────────────────────────────┐              ┌──────────────────────────────────────┐
│ 공식 RSS 12개 피드 수집        │              │ RSS 재수집 → 새 헤드라인만 추림           │
│ (선택) Claude 웹검색으로 보강   │              │ Jev: 헤드라인마다 Noul 2개                │
│ Claude → Perspective JSON     │              │   material_i: 관점에 없는 중대 변화인가?    │
│  macro_bias / risk_appetite   │              │   shock_i:    돌발 시장 쇼크인가?          │
│  today_view / event_risk      │◄─ 갱신 ──────│ 코드: material ≥ 2건 또는 shock ≥ 1건     │
│  scheduled_events / stance    │              │   → Claude가 이전 관점을 바탕으로 갱신      │
└──────────────┬───────────────┘              │   shock인데 갱신 실패 → 신규진입 60분 차단   │
               ▼                              └──────────────────────────────────────┘
   매 사이클 state["perspective"] 로 Jev에 주입
   Jev 추가 질문: macro_against_long / macro_against_short (Noul)
   정책(코드): event_risk=high 차단, trading_stance 반대 방향 차단, 쇼크 중 차단
```

- **생성 백엔드**: 기본은 `PERSPECTIVE_BACKEND=cli`로, 설치된 Claude Code CLI(`claude -p`)를 비대화형으로 호출합니다.
  로그인 세션을 그대로 쓰므로 API 키가 필요 없고, `--json-schema`로 스키마를 강제하며 WebSearch 도구로 직접 조사합니다.
  실측: 검색 6회 포함 약 45초, 약 $0.8 (상한 `PERSPECTIVE_CLI_BUDGET_USD`). `api`로 바꾸면 Anthropic SDK(`claude-opus-5`,
  구조화 출력, 서버측 웹검색)를 씁니다.
- **뉴스 소스**: 발행사가 배포용으로 제공하는 공식 RSS만 사용합니다(CoinDesk, Cointelegraph, The Block, Bitcoin Magazine,
  CNBC 3종, MarketWatch, Yahoo Finance, 연준·SEC·BLS 보도자료). 비공식 검색 엔드포인트처럼 차단될 위험이 없고 키도 필요 없습니다.
  `NEWS_FEEDS`로 이름 또는 RSS URL을 지정해 교체할 수 있습니다.
- **이벤트 기반 점검**: 1분봉이 `PRICE_SHOCK_WINDOW_MIN`분 동안 `PRICE_SHOCK_PCT`% 이상 움직이면 주기와 무관하게 즉시 스크리닝합니다.
- **비용**: 관점 생성은 하루 1~수회의 Claude 호출뿐이고, 10분마다 도는 감시는 Jev만 씁니다(40건 스크리닝 ≈ 5k 토큰).
- **Claude를 쓸 수 없을 때**: `claude` CLI가 없고(`cli` 백엔드) API 키도 없으면(`api` 백엔드) 관점 레이어만 꺼지고
  지표 기반 매매는 그대로 돕니다. Jev의 쇼크 감지는 계속 동작해 신규 진입을 `SHOCK_BLOCK_MINUTES` 동안 막습니다.
  `cli` 백엔드는 `.env`의 `ANTHROPIC_API_KEY`를 자동으로 무시하고 로그인 세션을 씁니다.

```bash
python -m jev_trade perspective build   # 지금 즉시 Claude로 관점 생성
python -m jev_trade perspective show    # 저장된 관점 JSON
python -m jev_trade perspective jev     # Jev가 실제로 읽는 블록
python -m jev_trade news fetch          # 현재 헤드라인 목록
python -m jev_trade news check          # Jev 스크리닝 1회 실행
```

기록 파일: `logs/perspective.json`(현재), `logs/perspective_history.jsonl`(이력), `logs/news_checks.jsonl`(스크리닝 결과),
`logs/news_seen.json`(평가 완료 헤드라인), `logs/monitor_state.json`(마지막 점검·쇼크 차단 시각).

## 타임프레임

| 요청 | 처리 |
| --- | --- |
| 1m, 5m, 15m, 30m, 1h, 1d, 1w | Binance 네이티브 |
| 10m | 5m 캔들을 리샘플링 |
| 5h | 1h 캔들을 리샘플링 |
| 1y | Binance에 연봉 캔들이 없으므로 **일봉 1500개 + 주봉 전체로 만든 장기 컨텍스트**: 역대 고점 대비 위치, 200주 이동평균 대비 위치와 주봉 EMA 정렬, 연도별·2년·3년 수익률, 최근 3개월 월봉 방향, 52주 레인지, 1/3/6개월 수익률, 200일 EMA, 낙폭 |

각 타임프레임은 마감된 캔들만 사용합니다(형성 중인 마지막 캔들은 제외). 실시간 정보는 `market` 블록으로 따로 전달됩니다.

**이력 깊이와 EMA 정확도**: 타임프레임마다 캔들 1000개(일봉 1500개, 주봉은 존재하는 전부 약 370개)를 받습니다.
EMA는 유한 구간에서 가중치를 정규화하는 방식(`ewm(adjust=True)`)이라 시작값 편향이 없고, 2500개 캔들로 완전히
워밍업한 재귀 EMA와 비교해 EMA200 오차가 0.001% 이하입니다(300개만 쓰던 초기 버전은 일봉에서 약 1% 편차).
한 사이클의 데이터 수집은 약 4초입니다.

## 설치와 실행

```bash
pip install -r requirements.txt
cp .env.example .env.local   # 값 채우기 (TYPESAFE_API_KEY 필수, Binance 키는 실거래 시)
```

```bash
python -m jev_trade state              # Jev에 보낼 state만 출력 (API 호출 없음)
python -m jev_trade once               # 1회 판단 (주문 없음)
python -m jev_trade once --execute     # 1회 판단 + 주문 (DRY_RUN=true면 페이퍼)
python -m jev_trade run                # 루프: DECISION_TIMEFRAME 캔들 마감마다 판단
python -m jev_trade replay --steps 100 # 최근 100개 캔들 워크포워드 리플레이 (페이퍼)
python -m jev_trade replay --steps 300 --with-perspective   # Claude 웹검색으로 날짜별 관점까지 복원
python -m jev_trade evaluate --set MIN_ENTRY_PROB=0.55      # 기록된 Jev 답에 다른 임계값 적용 (Jev 호출 없음)
```

기본값은 `DRY_RUN=true`(페이퍼 트레이딩)입니다. 실거래 순서:

1. `DRY_RUN=true`로 며칠 돌리며 `logs/decisions.jsonl`을 보고 임계값을 조정
2. `BINANCE_TESTNET=true` + 테스트넷 키로 주문 경로 검증
3. `DRY_RUN=false` + 실계좌 키 (원웨이 포지션 모드, 격리 마진 권장)

### 테스트넷 검증

Binance 선물 테스트넷은 **Demo Trading**으로 통합되었습니다. 예전 주소 `testnet.binancefuture.com`은 `demo.binance.com`으로
리다이렉트되고, 공식 API 호스트는 `demo-fapi.binance.com`입니다(봇은 이 호스트로 주문을 보냅니다).

1. 일반 Binance 계정으로 https://demo.binance.com/en/futures/BTCUSDT 에 로그인합니다(가상 자금 자동 지급, 실자금과 분리).
2. Demo Trading 화면의 **API Key** 관리 메뉴(주문 패널 하단 또는 계정 메뉴)에서 키·시크릿을 발급해 `.env.local`에 넣습니다.
   ```
   BINANCE_TESTNET=true
   DRY_RUN=false
   BINANCE_API_KEY=...
   BINANCE_API_SECRET=...
   ```
   시세와 캔들은 계속 메인넷 공개 데이터를 쓰고(`MARKET_DATA_TESTNET=false`), 주문만 테스트넷으로 갑니다.
3. 주문 경로 스모크 테스트: 최소 명목(약 60 USDT) 롱을 열어 손절·익절 예약을 확인하고 즉시 청산합니다.
   ```bash
   python -m jev_trade testnet              # 왕복 후 RESULT: PASS 를 확인
   python -m jev_trade testnet --keep-open  # 포지션을 남겨 두고 봇이 관리하는지 보기
   python -m jev_trade run                  # 테스트넷에서 실제 루프
   ```
   `BINANCE_TESTNET=true`가 아니면 이 명령은 실행을 거부합니다.

   2026-09-22 Demo Trading에서 RESULT: PASS 확인. 이 과정에서 잡은 사실: Binance 선물의 손절·익절(STOP_MARKET /
   TAKE_PROFIT_MARKET)은 **조건부(algo) 주문**이라 일반 미체결 목록에 안 보이고 일반 `cancel_all`로 지워지지 않습니다.
   봇은 조건부 주문을 따로 조회·취소하고, 새 진입 전에 이전 포지션의 잔여 조건부 주문을 정리합니다.

## 튜닝 포인트 (.env)

| 변수 | 의미 |
| --- | --- |
| `MIN_ENTRY_PROB`, `MIN_DIRECTION_EDGE` | 선택된 방향의 확률 하한, 그리고 P(방향)−P(반대) 하한. 3지선다에서는 hold 확률이 confidence를 희석하므로 방향 격차가 실질 확신 지표 |
| `MIN_SETUP_SCORE` | `setup_quality` 기대 레벨 하한 (0 no edge … 3 strong) |
| `CHOPPY_MAX` | `choppy` Noul 상한 |
| `MIN_EXIT_PROB`, `THESIS_INVALIDATED_THRESHOLD` | 청산 게이트 |
| `LEVERAGE_TIERS`, `RISK_TIERS` | Jev `conviction` 등급(0 weak … 3 very strong)별 배수와 손절 시 손실 % |
| `STOP_LIQ_RATIO_MAX`, `MAX_COST_PCT_OF_MARGIN` | 배수 안전 가드: 손절이 청산 거리의 절반 안에 있어야 하고, 수수료·펀딩이 증거금의 10% 이하 |
| `ATR_STOP_MULT`, `ATR_TP_MULT` | 손절/익절 거리(판단 타임프레임 ATR 배수) |
| `MAX_POSITION_PCT`, `COOLDOWN_CANDLES`, `MAX_TRADES_PER_DAY` | 하드 리스크 한도 |

임계값을 바꿔도 Jev를 다시 호출할 필요가 없습니다. `decisions.jsonl`에 답변이 그대로 남으므로
같은 답에 다른 정책을 적용해 볼 수 있습니다.

## 확신 등급 기반 사이징과 배수

Jev에게 배수 숫자를 묻지 않습니다(숫자 계산은 Jev의 약점). 대신 `conviction` Score로 확신 등급을 묻고 코드가 매핑합니다.

| Jev 확신 | 손절 시 손실 (자본 대비) | 배수 |
| --- | --- | --- |
| weak | 0.5% | 10x |
| moderate | 1.0% | 20x |
| strong | 1.5% | 35x |
| very strong | 2.0% | 50x |

손익은 리스크%가 정하는 명목에서 나오고, 배수는 묶이는 증거금과 청산 거리만 정합니다. 코드는 등급을 정한 뒤
손절 거리가 청산 거리의 절반을 넘으면 배수를 내리고, 왕복 수수료와 펀딩이 증거금의 10%를 넘어도 내리며,
거래소 브래킷 최대 배수와 `MAX_POSITION_PCT`도 확인합니다. 진입 직전에 거래소 배수를 그 값으로 설정합니다.

진입 판단은 당일 단기(5분~1시간) 지평입니다. 15m~1h가 방향, 1m~10m가 타이밍을 정하고, 5h·1d·1w·1y는
거부권이 아니라 확신 등급을 올리거나 내리는 맥락입니다. 약한 확신은 진입을 막는 대신 작게 들어갑니다.

## 비용·지연

한 판단은 약 4.5k 입력 토큰(≈ $0.0002), Jev 지연 0.7초 안팎, 데이터 수집 1.5초 안팎입니다.
5분봉 기준 하루 288회 판단 ≈ 1.3M 토큰 ≈ $0.05.

## 리플레이와 평가

`replay`는 최근 N개 판단 캔들을 걸어가며 그 시점의 타임프레임을 재구성해 Jev에 묻고 페이퍼로 체결합니다.

- **펀딩 복원**: Binance 펀딩 이력을 받아 시점별 `market.funding_rate`를 채웁니다. 호가창은 과거 데이터가 없어 제외합니다.
- **1분봉 체결**: 다음 판단 캔들 안을 1분봉으로 걸으며 손절·익절을 검사합니다. 갭이 나면 봉 시가에 체결되고,
  한 봉에서 둘 다 닿으면 손절을 먼저 인정합니다(보수적).
- **슬리피지·수수료**: `REPLAY_SLIPPAGE_BPS`, `REPLAY_FEE_BPS`로 시장가 체결에 반영합니다.
- **관점 복원**: `--with-perspective`를 주면 Claude 공식 웹검색으로 각 날짜 기준의 관점을 생성해(`logs/replay_perspectives/`에 캐시)
  관점 게이트까지 함께 검증합니다. 제3자 뉴스 아카이브는 쓰지 않습니다.

`evaluate`는 `logs/decisions.jsonl`에 남은 Jev 원답을 다른 임계값으로 다시 정책에 통과시킵니다.
Jev를 다시 호출하지 않으므로 무료이고, 기록된 결정과의 일치율·거래 수·손익을 돌려줍니다.

## 실계좌 안전장치

- **헤지 모드 감지**: 시작 시 포지션 모드를 조회해 헤지 모드면 실행을 거부합니다. `ALLOW_HEDGE_MODE=true`로 켜면 모든 주문에
  `positionSide`를 붙여 동작하지만 실계좌에서 검증되지 않았습니다. 원웨이 모드를 권장합니다.
- **포지션 진입 시각**: 봇이 직접 연 포지션은 `runtime_state.json`에 시각을 기록합니다. 봇이 모르는 포지션은 최근 체결을
  역순으로 누적해 진입 시각을 복원하므로 Jev에 `held_for`가 정확히 전달됩니다.
- **쇼크 차단**: 뉴스 감시가 쇼크를 감지하면 관점이 갱신될 때까지(갱신 불가 시 `SHOCK_BLOCK_MINUTES`) 신규 진입을 막습니다.

## 남은 한계

- 리플레이는 부분 체결·지연·거래소 장애를 모델링하지 않습니다.
- Jev는 학습하지 않습니다. 거래 표본이 수백 건 쌓이면 Jev 확률을 피처로 하는 분류기를 얹는 것이 다음 단계입니다.
- Jev 입력은 영어입니다(정확도 최상). 사람이 읽는 로그 요약만 필요하면 출력 단계에서 번역하면 됩니다.
- 헤지 모드 주문 경로는 실계좌에서 검증되지 않았습니다. Claude `api` 백엔드는 키가 없어 모의 테스트만 했고, `cli` 백엔드는 실호출로 검증했습니다.
