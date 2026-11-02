# hogi-chat

SOOP 라이브 방송을 **읽고, 듣고, 반응하는** 채팅 파이프라인.
방송 채팅을 실시간으로 수집하고, BJ 음성을 전사해 방송 상황을 만들고, 그 상황에 맞는 시청자 채팅 후보를 AI가 만들면, 사람이 승인한 문장만 실제 방송 채팅으로 나가는 구조다.

개인 연구·실험 프로젝트다. 2026-10-23부터 10-27까지 약 4일간 실제 방송에 붙여 돌렸고, 비용 문제로 실시간 동작을 중단한 상태다(아래 "현재 상태").

## 무엇을 만들었나

```
 SOOP 라이브 방송
   │
   ├─ 오디오(HLS) ──► 실시간 전사 ──► transcript.jsonl   (BJ 발화, 문장 단위)
   ├─ 화면(썸네일) ─► 비전 모델(예외적) ─► scene.jsonl
   └─ 채팅(WebSocket) ─► recent.jsonl                     (시청자 채팅)
                              │
                              ▼
                        후보 생성 (LLM)
        제목 + 최근 채팅 + BJ 발화 + 말투 통계 + RAG + 중복 회피
                              │
                              ▼
                  Discord 카드 (버튼: 문장 선택 / 거부 / 첨언)
                              │
                     사람이 버튼 클릭 (승인)
                              │
                              ▼
                 SOOP WebSocket 로그인 → 실제 방송 채팅 전송
                              │
                              ▼
                     training.jsonl (승인/거부 판정 축적)
```

핵심은 **자동 전송이 아니라 사람이 고르는 구조**다. AI는 후보를 만들고, 승인은 사람이 하고, 시스템은 그 판정을 다시 프롬프트에 반영한다.

## 구성

| 경로 | 역할 |
| --- | --- |
| `src/relay.py` | SOOP 채팅 WebSocket 수집 → `recent.jsonl`. 익명 수신, 읽기 전용 |
| `src/remote/stt_live.py` | BJ 실시간 전사. HLS 2초 조각 → 24kHz PCM → 음량 VAD → 전사 세션. OCI에서 실행 |
| `src/remote/stt_oci.py` | 앞선 방식(20초 녹음 파일 전사). 화면 설명(썸네일+비전)도 여기서 |
| `src/remote/stt_compare.py` | 전사 모델 비교 실험(모델명 가린 채 심사 모델이 채점) |
| `src/compose.py` | 후보 생성기. 컨텍스트 조립, 프롬프트, 검증, RAG, 중복 방지, 상태 카드 |
| `src/perceive.py` | 방송 지각(썸네일 설명, 오디오 구간 전사). 초기 버전 |
| `src/store.py` | 후보·판정 저장소. flock 기반 읽기-수정-쓰기, 전송 콜백 |
| `src/reaction.py` | 후보 메시지의 ✅/❌ 리액션 판정 |
| `src/replycmd.py` | Discord 답장으로 승인/거부/첨언 |
| `src/gateway_server.py`, `src/bot_gateway.py` | 버튼·모달 interaction 처리(공개 엔드포인트 / 게이트웨이 두 방식) |
| `src/ws_send.py` | SOOP 로그인 후 WebSocket으로 채팅 전송 |
| `src/read_cookies.py` | 브라우저 프로필 쿠키에서 로그인 티켓 복호화(macOS) |
| `src/retrieval.py` | 유사 상황 검색(RAG) 색인 구축·질의 |
| `src/console.py` | 승인 콘솔(로컬 웹 UI). 후보의 근거 컨텍스트·프롬프트 전문·리플레이 |
| `src/ctl.py` | 수집 프로세스 시작/정지·상태 |
| `src/safety_test.py` | 인젝션·사칭·개인정보 대응 자체 점검 |
| `src/replay.py` | 녹화본으로 컨텍스트 주입 리플레이 테스트 |
| `research/archive/` | 대규모 채팅 아카이브(R2 parquet) 측정 스크립트 |
| `docs/`, `research/` | 설계 문서와 연구 기록 |

## 현재 상태 (2026-11-02 기준)

- 실시간 생성·전사는 **꺼져 있다.** 2026-10-27 01:49, 운영자 지시로 중단.
  이유는 비용: 전사 시간당 약 $0.97, AI 판단 시간당 약 $0.92.
- 자동 재개를 막아 두었다(`OFF_BY_OWNER` 파일이 있으면 systemd 유닛이 시작을 건너뛴다).
- 코드·문서·측정 결과는 남아 있다. 재개는 소유자 판단이다.

## 측정된 비용 (2026-10-26 집계)

- 실시간 전사: 공식 단가 분당 $0.017(1시간 $1.02). 실측 6.5시간 $6.29 → **시간당 $0.97**.
  배경음악 때문에 음량 감지가 거의 끊기지 않아 방송 시간 대부분이 과금됐다.
- AI 판단(후보 생성+가사 분리+수신자 판정): 하루 608회 호출 $5.96 → **시간당 $0.92**, 호출당 약 $0.01.
  비용의 2/3가 출력(생각 과정 포함, 평균 1,080토큰).
- 방송을 월 130시간 켠다고 가정하면 전사만 약 $126/월.

## 돌려 보려면

```bash
cp .env.example .env      # 값을 채운다 (git에 올리지 않는다)
pip install -r requirements.txt
export HOGI_BASE=~/hogi-chat        # 상태·로그·데이터가 쌓이는 곳

python3 src/ctl.py start           # 채팅 수집
python3 src/compose.py             # 후보 생성 (Discord 카드)
python3 src/console.py             # 승인 콘솔 http://localhost:8791
```

`config.py`가 환경변수로 모든 식별자를 받는다. 코드에는 개인 계정·채널 ID가 없다.

> ⚠️ 이 저장소에는 실제 채팅 로그·전사·판정 데이터가 없다. 수집 데이터는 개인정보와
> 타인 발화를 포함하므로 배포물에 넣지 않는다(`data/README.md`).

## 설계에서 배운 것 (요약)

- **프롬프트만으로는 규칙이 안 지켜진다.** 방송 규칙 위반은 프롬프트 지시와 별개로 코드 게이트로 막아야 했다.
- **승인 데이터는 쉽게 오염된다.** 승인된 문장이 과거 발화 복사본이면 "선호 학습"이 아니라 "복사 보상"이 된다.
- **전사 모델은 지시를 따른다더라도 노래 가사는 샌다.** 별도 처리(구간 판정)가 필요했다.
- **비용은 출력에서 나온다.** 판단 모델의 추론 토큰을 줄이는 게 입력을 줄이는 것보다 컸다.
- **실패는 대부분 상주 프로세스 쪽에서 온다.** 옛 모듈을 물고 있는 데몬, 방송이 꺼졌는데 재연결하는 루프.

자세한 내용: [`docs/architecture.md`](docs/architecture.md), [`research/lessons.md`](research/lessons.md)

## 문서

- [구조](docs/architecture.md)
- [SOOP 채팅 프로토콜](docs/soop-chat-protocol.md)
- [실시간 전사](docs/live-transcription.md)
- [후보 생성기](docs/candidate-composer.md)
- [승인과 전송](docs/approval-and-sending.md)
- [운영](docs/operations.md)
- 연구: [코퍼스 감사](research/corpus-audit.md) · [모델 학습 검토](research/model-training.md) · [GPU·비용 조사](research/gpu-and-cost.md) · [실패 기록](research/lessons.md)

## 라이선스

AGPL-3.0. 이 저장소는 개인 방송 채널에서 쓰는 도구이며, 다른 채널에 쓰려면 그 채널 운영자의 허락을 받아야 한다.
