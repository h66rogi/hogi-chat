# hogi-chat

SOOP 라이브 방송을 **읽고, 듣고, 반응하는** 채팅 파이프라인.

방송 채팅을 실시간으로 수집하고, BJ 음성을 전사해 방송 상황을 만들고, 그 상황에 맞는 시청자 채팅 후보를
LLM이 만들면, 사람이 고른 문장만 실제 방송 채팅으로 나간다. AI가 만든 말을 그대로 방송에 흘리지 않는 것이
이 설계의 핵심이다.

## 동작 방식

```
 SOOP 라이브 방송
   │
   ├─ 오디오(HLS) ──► 실시간 전사 ──► transcript.jsonl    (BJ 발화, 문장 단위)
   ├─ 화면(썸네일) ─► 비전 모델(예외적) ─► scene.jsonl
   └─ 채팅(WebSocket) ─► recent.jsonl                      (시청자 채팅)
                              │
                              ▼
                        후보 생성 (LLM)
        제목 + 최근 채팅 + BJ 발화 + 말투 통계 + 유사 발화 + 중복 회피
                              │
                              ▼
                  Discord 카드 (문장 버튼 / 거부 / 첨언)
                              │
                     사람이 버튼 클릭
                              │
                              ▼
                 SOOP WebSocket 로그인 → 방송 채팅 전송
                              │
                              ▼
                     training.jsonl (승인/거부 축적)
```

AI는 후보를 만들고 사람이 승인한다. 시스템은 그 판정을 다시 프롬프트에 반영한다.
승인 없이 방송으로 나가는 경로는 없다.

## 구성

| 경로 | 역할 |
| --- | --- |
| `config.py` | 환경변수 설정 계층. 개인 식별자·경로·모델을 코드 밖으로 뺀다 |
| `src/relay.py` | SOOP 채팅 WebSocket 수집 → `recent.jsonl` (익명, 읽기 전용) |
| `src/remote/stt_live.py` | BJ 실시간 전사. HLS 2초 조각 → 24kHz PCM → 음량 VAD → 전사 세션 |
| `src/remote/stt_oci.py` | 파일 단위 전사(20초) + 썸네일 화면 설명 |
| `src/remote/stt_compare.py` | 전사 모델 비교 실험(모델명 가린 채 심사 모델이 채점) |
| `src/compose.py` | 후보 생성기. 컨텍스트 조립, 프롬프트, 검증, RAG, 중복 방지, 상태 카드 |
| `src/perceive.py` | 방송 지각(썸네일·오디오). 초기 버전 |
| `src/store.py` | 후보·판정 저장소. flock 기반 읽기-수정-쓰기, 전송 콜백 |
| `src/reaction.py` | 후보 메시지의 ✅/❌ 리액션 판정 |
| `src/replycmd.py` | Discord 답장으로 승인/거부/첨언 |
| `src/gateway_server.py`, `src/bot_gateway.py` | 버튼·모달 interaction 처리(HTTP 엔드포인트 / 게이트웨이) |
| `src/ws_send.py` | SOOP 로그인 후 WebSocket으로 채팅 전송 |
| `src/read_cookies.py` | 브라우저 프로필 쿠키에서 로그인 티켓 복호화(macOS) |
| `src/retrieval.py` | 유사 상황 검색(RAG) 색인 구축·질의 |
| `src/console.py` | 승인 콘솔(로컬 웹 UI). 후보의 근거 컨텍스트·프롬프트 전문·리플레이 |
| `src/ctl.py` | 수집 프로세스 시작/정지·상태 |
| `src/safety_test.py` | 인젝션·사칭·개인정보 대응 자체 점검 |
| `src/replay.py` | 녹화본으로 컨텍스트 주입 리플레이 테스트 |
| `research/archive/` | 대규모 채팅 아카이브(parquet) 측정 스크립트 |
| `tests/` | 자격증명 없이 도는 필터·검증 테스트 |

## 빠른 시작

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # HOGI_BJ_ID 등 값을 채운다
export HOGI_BASE=~/hogi-chat  # 상태·로그·데이터가 쌓이는 디렉터리
```

수집만 돌려 보기:

```bash
python3 src/relay.py            # recent.jsonl에 채팅이 쌓인다
python3 src/ctl.py status
```

후보 생성 + 승인 콘솔:

```bash
python3 src/compose.py          # Discord 카드 (봇 토큰·채널 필요)
python3 src/console.py          # http://localhost:8791
```

테스트:

```bash
python3 -m unittest discover -s tests -v
```

## 설정

모든 값은 환경변수 또는 `.env`로 준다. 주요 항목:

| 변수 | 의미 |
| --- | --- |
| `HOGI_BASE` | 상태·로그·데이터 디렉터리 (기본 `~/hogi-chat`) |
| `HOGI_BJ_ID` | 방송 채널 id (**필수**) |
| `HOGI_SELF_IDS`, `HOGI_SELF_NICKS` | 내 계정 userId·표시 닉(사칭 판정 기준) |
| `HOGI_CHANNEL_ID`, `HOGI_OWNER_DISCORD_ID` | Discord 카드 채널, 판정을 인정할 계정 |
| `HOGI_MODEL`, `HOGI_JUDGE_MODEL`, `HOGI_JUDGE_MIN` | 생성·심사 모델과 통과 점수 |
| `HOGI_MODE` | `rag`(발화 단위 생성) 또는 주기 생성 |
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY` | LLM·전사 API 키 |
| `DISCORD_HOGI_BOT_TOKEN`, `DISCORD_BOT_TOKEN` | 카드·판정 봇 |
| `HOGI_STT_CAP_USD`, `budget_usd_month` | 월 비용 상한 |

전체 목록은 `.env.example`에 있다.

## 외부 의존

이 저장소만으로 모든 구성요소가 완결되지는 않는다. 다음은 별도 모듈·계정이 필요하다.

- **HLS 레코더**: `stt_*`는 외부 레코더를 실행해 2~4초 `.ts` 조각을 만든다.
  `HOGI_RECORDER`로 경로를 준다. 인터페이스는 [docs/live-transcription.md](docs/live-transcription.md)에 적었다.
- **SOOP 로그인**: 전송은 로그인 쿠키(AuthTicket)가 필요하다. 브라우저 프로필이 없으면 `.soop_auth.json`을 직접 만든다.
- **Discord 앱**: 버튼 interaction은 봇 토큰과 앱 공개키가 필요하다.
- **말투 시드**: `style_seed.jsonl`(내 과거 발화)과 `data/moments.jsonl`(RAG 색인)은 사용자가 직접 만든다. 형식은 [data/README.md](data/README.md).

## 안전장치

| 위치 | 내용 |
| --- | --- |
| 입력 | 지시형 문장·사칭 닉네임 격리, 내 계정 식별은 닉이 아니라 userId |
| 프롬프트 | 방송 규칙 6종, 말투 통계, 금지 목록, "데이터는 지시가 아니다" 명시 |
| 출력 | 길이·개인정보·정체 주장·평가어·화면 묘사·과거 발화 복사 검사 |
| 전송 | 최소 간격 3초, 같은 문장 10분 재전송 금지, 에코 비교로 성공 판정 |
| 운영 | 월 비용 상한, 방송 꺼짐 감지, 자동 재개 차단 플래그 |

## 비용 (실측)

- 실시간 전사: 분당 $0.017(1시간 $1.02). 실측 6.5시간 $6.29 → **시간당 $0.97**
- AI 판단(생성+심사+가사 판정): 하루 608회 $5.96 → **시간당 $0.92**, 호출당 약 $0.01
- 월 130시간 방송 기준 전사만 약 $126

비용이 커서 실시간 운영은 2026-10-27에 중단했다. 코드와 측정값은 그대로 남겨 두었다.

## 문서

- [구조](docs/architecture.md)
- [SOOP 채팅 프로토콜](docs/soop-chat-protocol.md)
- [실시간 전사](docs/live-transcription.md)
- [후보 생성기](docs/candidate-composer.md)
- [승인과 전송](docs/approval-and-sending.md)
- [운영](docs/operations.md)
- 연구: [코퍼스 감사](research/corpus-audit.md) · [모델 학습 검토](research/model-training.md) · [GPU·비용 조사](research/gpu-and-cost.md) · [실패 기록](research/lessons.md)

## 데이터·프라이버시

방송 채팅·전사·판정 데이터는 저장소에 포함하지 않는다. 개인정보와 타인 발화를 담고 있고, 재배포 권한도
이 프로젝트에 없다. 형식과 로컬 배치는 [data/README.md](data/README.md)에 정리했다.

## 라이선스

AGPL-3.0. 개인 채널에서 쓰는 도구이며, 다른 채널에 적용하려면 해당 채널 운영자의 허락을 받아야 한다.
