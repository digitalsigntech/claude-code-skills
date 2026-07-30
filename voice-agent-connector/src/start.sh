#!/bin/bash
# Start the voice agent connector + an HTTPS tunnel, then print the pairing QR.
#
#   ./start.sh              cloudflared quick tunnel (zero-config, URL changes
#                           per run — re-scan the QR after a restart)
#   PUBLIC_URL=https://you.example.com ./start.sh
#                           your own stable HTTPS (reverse proxy to :8484)
#
# Needs: python3, qrencode, and cloudflared in PATH or next to this script
# (unless PUBLIC_URL is set).
cd "$(dirname "$0")"
set -e

pkill -f "python3 connector.py$" 2>/dev/null || true
nohup python3 connector.py >> connector.log 2>&1 &
sleep 1
grep -q listening <(tail -2 connector.log) || { echo "connector failed — see connector.log"; exit 1; }

if [ -n "$PUBLIC_URL" ]; then
  HOST="$PUBLIC_URL"
else
  CF=$(command -v cloudflared || echo ./cloudflared)
  [ -x "$CF" ] || { echo "cloudflared not found — install it or set PUBLIC_URL"; exit 1; }
  pkill -f "tunnel --url http://127.0.0.1:8484" 2>/dev/null || true
  : > tunnel.log
  nohup "$CF" tunnel --url http://127.0.0.1:8484 >> tunnel.log 2>&1 &
  for i in $(seq 1 30); do
    HOST=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
    [ -n "$HOST" ] && break
    sleep 1
  done
  [ -n "$HOST" ] || { echo "no tunnel URL — see tunnel.log"; exit 1; }
fi

echo "$HOST" > url.txt
echo "connector up: $HOST/<path>/hook"
python3 qr.py
