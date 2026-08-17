#!/usr/bin/env bash
set -euo pipefail

POD="${POD:-vllm11-1}"
MODEL="${MODEL:-${VLLM_MODEL:-Qwen/Qwen3-0.6B}}"
PORT="${PORT:-8000}"
PROMPT="${PROMPT:-Hello from testhook}"

# Copy the import hook into the pod
oc cp importhook.py "$POD":/home/sitecustomize.py

# Start the server and send a simple inference once it responds
oc rsh "$POD" bash -s <<EOF
set -euo pipefail

echo "Launching vLLM server (model=${MODEL}, port=${PORT})..."
HF_HOME=/model-cache PYTHONPATH=/home nohup vllm serve --tensor-parallel 8 --model "$MODEL" --host 0.0.0.0 --port "$PORT" >/tmp/vllm-serve.log 2>&1 &
SERVE_PID=\$!

cleanup() {
  if kill -0 "\$SERVE_PID" >/dev/null 2>&1; then
    echo "Stopping vLLM server (pid=\$SERVE_PID)..."
    kill "\$SERVE_PID" >/dev/null 2>&1 || true
    wait "\$SERVE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "Waiting for server to respond..."
for _ in {1..120}; do
  if curl -sf "http://localhost:${PORT}/v1/models" >/dev/null 2>&1; then
    echo "Server is up"
    break
  fi
  sleep 1
done

echo "Sending test inference request (max_tokens=200)..."
curl -s -X POST "http://localhost:${PORT}/v1/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"${PROMPT}\",\"max_tokens\":200}"
echo
sleep 5
awk '/===== begin profiler output/,/===== end profiler output/' /tmp/vllm-serve.log
EOF
