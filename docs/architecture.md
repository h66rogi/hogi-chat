# 구조

## 컴포넌트와 데이터 흐름

```
                 ┌──────────────┐
 SOOP 채팅 ─────►│ relay.py     │──► recent.jsonl   {t, nick, text, uid?}
 (WebSocket)     └──────────────┘
                 ┌──────────────┐
 SOOP 오디오 ───►│ stt_live.py  │──► transcript.jsonl {ts, end, text, ...}
 (HLS 2초 조각)  └──────────────┘
                 ┌──────────────┐
 SOOP 썸네일 ───►│ stt_oci.py   │──► scene.jsonl      (예외적, BJ가 보여줄 때만)
                 └──────────────┘

                 ┌──────────────┐    ┌───────────────┐
                 │ compose.py   │───►│ Discord 카드  │──► 사람 승인
                 └──────┬───────┘    └───────────────┘
                        │ candidates.jsonl
                        ▼
                 ┌──────────────┐    ┌───────────────┐
                 │ store.py     │───►│ ws_send.py    │──► SOOP 채팅
                 └──────┬───────┘    └───────────────┘
                        ▼
                 training.jsonl (승인/거부)
```

## 상태 파일 (`HOGI_BASE`, 기본 `~/hogi-chat`)

| 파일 | 내용 | 쓰는 쪽 |
| --- | --- | --- |
| `state.json` | `enabled`(전송), `dry_run`, `channel_id`, `live`, `perceive`, `budget_usd_month` | 사람/콘솔 |
| `recent.jsonl` | 수집한 채팅 | relay |
| `transcript.jsonl` | BJ 발화 문장 | stt |
| `scene.jsonl` | 화면 설명 | stt_oci |
| `title.txt` | 방송 제목 | relay |
| `candidates.jsonl` | 생성 후보 + 당시 컨텍스트 + message_id + 판정 | compose, store |
| `picks.json` | 카드별 후보 묶음(버튼 토큰) | compose |
| `training.jsonl` | 승인/거부 판정 | store |
| `sent.jsonl` | 실제 전송 이력 | ws_send |
| `usage.jsonl` | 모델 호출별 비용 | compose, stt |
| `status.json` | 현재 상태 카드 message_id | compose |
| `style_seed.jsonl` | 내 과거 발화(말투 시드) | 오프라인 |
| `.relay.lock`, `.store.lock` | 단일 실행·동시 쓰기 방지 | relay, store |

## 설계 판단

### 1. 수집과 생성을 분리한다
수집(`relay.py`)은 오래 살아 있어야 하고, 생성(`compose.py`)은 자주 고쳐야 한다.
분리해 두면 프롬프트를 바꿀 때 수집을 끊지 않는다. 두 프로세스는 파일로만 통신한다.

### 2. 파일 + flock
SQLite 없이 JSONL + `fcntl.flock`으로 충분했다. 초당 수 건 수준이고, 여러 프로세스(생성·콘솔·게이트웨이)가
같은 후보를 동시에 만지는 경우만 막으면 됐다. `store.py`가 읽기-수정-쓰기를 락 안에서 한다.

### 3. 생성은 "한 줄"이 아니라 "여러 줄 + 심사"
한 번 호출에 후보 6줄을 뽑고, 심사 호출이 그중 지금 상황에 맞는 하나를 고르거나 침묵을 택한다.
후보를 여러 개 두는 이유는 (a) 사람이 고를 여지를 남기고 (b) 심사가 걸러낼 재료가 필요해서다.

### 4. 상태 카드 하나만 유지
Discord 채널에는 후보 카드가 계속 쌓이면 안 된다. `status.json`에 마지막 카드 ID를 두고
새 카드를 올리기 전에 지운다. "안 올림"도 카드에 남겨 정상 동작 여부를 알 수 있게 했다.

### 5. 단일 인스턴스
relay가 두 개 뜨면 같은 채팅이 두 번 수집된다. `.relay.lock`에 `LOCK_EX | LOCK_NB`로 잡고
이미 잡혀 있으면 종료한다.

### 6. 실패는 계속, 그러나 비용은 태우지 않는다
방송이 꺼져 있으면 재연결 루프가 전사 비용을 태운다. 실제 사고: 방송이 03:19에 끝났는데
생성이 90초마다 상태 카드를 갈아끼우고, 전사가 5초마다 세션을 다시 열어 로그와 과금이 계속됐다.
그래서 전사(`stt_live.py`)는 시작 전에 `player_live_api`의 `RESULT`를 보고, 꺼져 있으면 세션을 열지 않고
45초 뒤 다시 확인한다. 생성 쪽은 최근 채팅이 없으면 "방송 꺼짐 또는 한산"으로 판단해 프롬프트 검사만 하고
카드를 남기지 않는다.


## 배포 (서버)

가정용 회선에서는 SOOP 오디오가 1초 안에 끊겼다(9세션 45초 동안 0.5초 수신).
같은 코드가 데이터센터 회선에서는 30초에 712KB를 정상 수신했다. 그래서 **수집·전사·생성은 서버**에서 돌리고,
승인 콘솔은 사설망으로 접근했다.

systemd 유닛(요약): `hogi-relay`, `hogi-stt`(또는 `hogi-stt-live`), `hogi-compose`, `hogi-console`, `hogi-gateway`, `hogi-tunnel`.
설치 스크립트는 `src/remote/install_units.sh`.

## 안전 게이트가 붙는 위치

| 위치 | 목적 |
| --- | --- |
| 입력(채팅) | 지시형 문장·사칭 닉네임 격리 |
| 프롬프트 | 방송 규칙, 말투 통계, 금지 목록 |
| 출력 검증 | 길이·개인정보·정체 주장·평가어·화면 묘사·과거 발화 복사 |
| 전송 직전 | 최소 간격(3초), 같은 문장 10분 재전송 금지 |
| 운영 | 예산 상한, 방송 꺼짐 감지, 자동 재개 차단 플래그 |

각 게이트가 실제로 무엇을 막는지는 [`docs/candidate-composer.md`](candidate-composer.md),
[`docs/approval-and-sending.md`](approval-and-sending.md)에 적었다.
