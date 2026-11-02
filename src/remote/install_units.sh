#!/bin/sh
# hogi-chat systemd 유닛 설치 (OCI vlife-rec-1). 시작은 별도로 한다.
set -e
APP=/home/ubuntu/hogi-chat

unit() {
  name="$1"; desc="$2"; cmd="$3"; extra="$4"
  sudo tee /etc/systemd/system/$name.service >/dev/null <<EOF
[Unit]
Description=$desc
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1
ExecStart=$cmd
Restart=always
RestartSec=10
Nice=10
$extra

[Install]
WantedBy=multi-user.target
EOF
}

unit hogi-stt     "hogi-chat BJ 전사 (SOOP 오디오 -> GPT 오디오)" "/usr/bin/python3 $APP/stt_oci.py" "CPUQuota=60%
MemoryMax=600M"
unit hogi-relay   "hogi-chat 채팅 수집 (SOOP chat -> recent.jsonl)" "/usr/bin/python3 $APP/relay.py" "MemoryMax=200M"
unit hogi-compose "hogi-chat 후보 생성 + Discord 버튼 카드" "/usr/bin/python3 $APP/compose.py" "MemoryMax=300M"
unit hogi-console "hogi-chat 승인 콘솔 (tailnet 전용)" "/usr/bin/python3 $APP/console.py --port 8791 --host 127.0.0.1" "MemoryMax=200M"
unit hogi-gateway "hogi-chat Discord 버튼 게이트웨이" "/usr/bin/python3 $APP/gateway_server.py --port 8793" "MemoryMax=200M"
unit hogi-tunnel  "hogi-chat quick tunnel + Discord 엔드포인트 자동 갱신" "/usr/bin/python3 $APP/tunnel_oci.py" "ExecStopPost=/usr/bin/docker rm -f hogi-tunnel"

sudo systemctl daemon-reload
echo installed
