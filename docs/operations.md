# 운영

## 배포 형태

수집·전사·생성·콘솔은 리눅스 서버(OCI)에서 systemd로 돌고, 카드·승인은 Discord를 쓴다.
맥에서는 개발·리플레이만 했다(SOOP 오디오가 맥/집 회선에서 끊겨서 운영 불가).

유닛 목록(`src/remote/install_units.sh`):

| 유닛 | 역할 |
| --- | --- |
| `hogi-relay` | 채팅 수집 |
| `hogi-stt` / `hogi-stt-live` | 파일 전사 / 실시간 전사 |
| `hogi-compose` | 후보 생성 + 상태 카드 |
| `hogi-console` | 승인 콘솔(tailnet 전용) |
| `hogi-gateway` | Discord 버튼 interaction 엔드포인트 |
| `hogi-tunnel` | quick tunnel + Discord 엔드포인트 URL 자동 갱신 |

각 유닛은 `Restart=always`, `MemoryMax`, `CPUQuota`로 묶어 두었다. 저사양 CPU에서 전사·생성이
서로를 굶기지 않게 하려는 것이다.

## 비용 상한

- `state.json`의 `budget_usd_month`(기본 100) — 넘으면 생성 중단
- `HOGI_STT_CAP_USD`(기본 30) — 전사 월 상한
- `usage.jsonl`에 호출·과금 초 단위 기록. 콘솔에서 이번 달 사용액 확인

상한은 "안전장치"가 아니라 **사고 방지**다. 실제로 크레딧 소진과 방송 종료 후 재연결 루프로
쓸데없는 과금이 발생했다.

## 자동 재개 차단

실시간 후보 생성을 끌 때, 재부팅·배포로 다시 살아나지 않게 세 겹으로 막았다.

1. 앱 디렉터리에 `OFF_BY_OWNER` 파일 — 있으면 서비스가 시작하지 않는다
2. systemd 유닛 비활성화
3. 콘솔의 생성 토글 OFF

`OFF_BY_OWNER`가 있으면 일부러 시작 명령을 보내도 건너뛴다. 다시 켜는 것은 소유자 판단이다.

## 장애 사례

### 1) OpenAI 크레딧 소진 → 9시간 무음
- 증상: 전사 0건, 4초마다 재연결, 후보 생성 실패. 로그만 쌓이고 방송 상태 카드는 멀쩡해 보였다.
- 원인: `credit_balance_exhausted`.
- 조치: 키 교체(서버 설정 백업 후), 1Password에 보관, 재연결 루프에서도 실패를 눈에 띄게 남김.
- 교훈: **조용한 실패가 가장 비싸다.** "전사 150초 이상 없음"을 상태 카드에 표시하게 했다.

### 2) 방송이 끝났는데 계속 돈다
- 증상: 방송 종료 03:19 이후에도 90초마다 상태 카드 갱신, 5초마다 전사 세션 재연결, 계속 과금.
- 조치: 방송 상태 확인을 넣어 꺼져 있으면 세션을 열지 않고 대기. 상태 카드도 한 번만 갱신.

### 3) 옛 모듈을 물고 있는 데몬
- 증상: 전송 경로를 WebSocket으로 바꿨는데 승인 3건이 여전히 브라우저(Aside) 경로로 나갔다.
- 원인: 실행 중인 데몬이 옛 `reaction.py`를 메모리에 물고 있었다.
- 조치: 데몬 재시작. 이후 전송 경로 코드를 바꿀 때는 **재시작까지** 한다.
- 교훈: 코드 변경의 완료 조건은 "파일 저장"이 아니라 "프로세스가 새 코드로 도는 것"이다.

### 4) 같은 서버에서 SOOP 수신 세션 2개
- 증상: 두 번째 세션이 `IncompleteReadError`.
- 조치: 비교 실험은 운영 세션이 받은 조각을 복사해서 수행.

### 5) Discord 엔드포인트가 Cloudflare 뒤에서 PING 실패
- 증상: interaction 엔드포인트 검증 실패.
- 조치: 공개 HTTPS 엔드포인트 대신 게이트웨이 WebSocket 클라이언트(`bot_gateway.py`)로 전환.
  또는 quick tunnel을 띄우고 URL이 바뀔 때마다 Discord 앱 설정을 자동 갱신(`tunnel_oci.py`).

## 로그

| 파일 | 내용 |
| --- | --- |
| `relay.log` | 연결·수신·전송 |
| `compose.log` | 프롬프트 판단, 검증 탈락 사유, 심사 점수 |
| `reaction.log` | 리액션 판정, 실제 전송 결과 |
| `console.log` | 콘솔 조작, 판정 호출자 |
| `gateway.log` | interaction 처리 |
| `stt.log` | 전사 세션·전사 결과·버림 |
| `usage.jsonl` | 비용 |

로그에는 채팅 본문이 일부 남는다. 저장소·공유물에 그대로 넣지 않는다.

## 재개할 때 확인할 것

1. SOOP 플레이어가 바뀌지 않았는지 (`CHATNO`·`FTK`·패킷 형식)
2. 전사 단가와 모델명(`gpt-live-transcribe` 계열) 변경 여부
3. 예산 상한(`budget_usd_month`, `HOGI_STT_CAP_USD`)
4. 승인 없이 전송되는 경로가 없는지 (`state.json`의 `live`, `enabled`)
5. 계정 상태: 쿠키 만료, 구독 표시, 2차 인증
