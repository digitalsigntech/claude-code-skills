#!/usr/bin/env bash
# Start two local, OpenAI-compatible model servers: a chat/instruct model and an
# embedding model. Localhost only — nothing is exposed off-box.
#
# This example uses llama.cpp's `llama-server`, but ANY OpenAI-compatible backend
# works (vLLM, Ollama, etc.). If you already run your endpoints elsewhere, skip this
# script and just point DOCPIPE_CHAT_URL / DOCPIPE_EMB_URL at them (see config.py).
#
# Configure via environment (or edit the defaults below):
#   LLAMA_BIN_DIR  directory containing `llama-server`
#   CHAT_MODEL_GGUF / EMB_MODEL_GGUF  paths to your GGUF model files
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_BIN_DIR="${LLAMA_BIN_DIR:-$HOME/llama.cpp/build/bin}"
LLAMA="$LLAMA_BIN_DIR/llama-server"
export LD_LIBRARY_PATH="$LLAMA_BIN_DIR:${LD_LIBRARY_PATH:-}"
MODELS="${MODELS_DIR:-$HERE/models}"
LOGS="${LOGS_DIR:-$HERE/logs}"
# Bring your own models. Examples: a 7B instruct model + a small embedding model.
CHAT_MODEL_GGUF="${CHAT_MODEL_GGUF:-$MODELS/chat-model.gguf}"
EMB_MODEL_GGUF="${EMB_MODEL_GGUF:-$MODELS/embed-model.gguf}"
CHAT_PORT="${CHAT_PORT:-18182}"
EMB_PORT="${EMB_PORT:-18183}"
mkdir -p "$LOGS"

[ -x "$LLAMA" ] || { echo "llama-server not found at $LLAMA (set LLAMA_BIN_DIR)"; exit 1; }

start() {  # name port model args...
  local name="$1" port="$2"; shift 2
  if curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "$name already up on :$port"; return
  fi
  echo "starting $name on :$port ..."
  nohup "$LLAMA" "$@" --host 127.0.0.1 --port "$port" > "$LOGS/$name.log" 2>&1 &
  echo $! > "$LOGS/$name.pid"
}

# Chat model. --parallel 1 is REQUIRED for RAG: without it llama-server defaults to
# multiple slots, splitting the context into small chunks, which fails long RAG
# prompts. A large context (-c 32768) lets aggregation queries (k=48, long prompts)
# fit. Tune -ngl / -b / -ub / -t for your hardware.
start chat "$CHAT_PORT" \
  -m "$CHAT_MODEL_GGUF" \
  --alias local-chat -c 32768 --parallel 1 -ngl 99 -t 8 -fa off -b 2048 -ub 64 --cache-ram 0

# Embedding model. Runs on CPU (-ngl 0) here so the whole GPU stays free for chat;
# adjust to taste. -ub must be large enough for your longest chunk.
start emb "$EMB_PORT" \
  -m "$EMB_MODEL_GGUF" \
  --alias local-embed --embeddings -c 8192 -b 4096 -ub 4096 -ngl 0 -t 8 --cache-ram 0

echo "waiting for health..."
for name_port in chat:$CHAT_PORT emb:$EMB_PORT; do
  name="${name_port%%:*}"; port="${name_port##*:}"
  for i in $(seq 1 60); do
    if curl -s "http://127.0.0.1:$port/health" | grep -q '"status"' 2>/dev/null || \
       curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" 2>/dev/null | grep -q 200; then
      echo "  $name UP (:$port)"; break
    fi
    sleep 2
    [ "$i" -eq 60 ] && echo "  $name did NOT come up — see $LOGS/$name.log"
  done
done
