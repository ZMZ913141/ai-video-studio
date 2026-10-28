#!/usr/bin/env bash
# ============================================================================
# 免 Docker 启动 —— 本机直跑 pipeline-service（n8n 可选）
#
# 为什么有这条路：dockerd 要 root 才能装/跑，而你的机器当前没装 docker。
# 但本栈里 **只有 n8n 是真正"容器友好"的**，pipeline-service 就是个普通
# Python 服务、ComfyUI 本来就跑在宿主机独占 GPU —— 三者都不需要容器。
#
#   bash bin/start-native.sh            # 启动 pipeline-service（8000 端口）
#   bash bin/start-native.sh --n8n      # 额外用 npx 起 n8n（5678 端口）
# ============================================================================
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$BASE/.venv"
PY="/home/qmr/.workbuddy/binaries/python/versions/3.13.12/bin/python3"

echo "== 1. 虚拟环境 =="
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$BASE/orchestration/pipeline-service/requirements.txt"
echo "   ✓ 依赖就绪"

echo "== 2. 加载配置 =="
set -a
[ -f "$BASE/orchestration/.env" ] && . "$BASE/orchestration/.env"
[ -f "$BASE/config/llm.env" ] && . "$BASE/config/llm.env"
set +a
echo "   ComfyUI=${COMFYUI_SERVER_URL:-http://localhost:8188}"
echo "   护栏水位 RAM≥${GUARD_MIN_RAM_GB:-12}G VRAM≥${GUARD_MIN_VRAM_GB:-6}G"

echo "== 3. 开工前资源预检 =="
"$VENV/bin/python" "$BASE/orchestration/comfy_guard.py" preflight || {
  echo "   ⚠ 资源不足，先释放后再试："
  echo "     python3 $BASE/orchestration/comfy_guard.py free"
  exit 1
}

echo "== 4. 启动 pipeline-service :8000 =="
cd "$BASE/orchestration/pipeline-service"
nohup "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8000 \
      >"$BASE/orchestration/pipeline.log" 2>&1 &
echo $! > "$BASE/orchestration/pipeline.pid"
sleep 3
curl -sS -m 5 http://127.0.0.1:8000/health && echo "" || echo "   （健康检查未通，看 pipeline.log）"

if [[ "${1:-}" == "--docker" ]]; then
  # ⚠ WorkBuddy 的 shell 会注入 DOCKER_HOST=unix:///run/user/1000/podman/podman.sock，
  #   那个 socket 根本不存在 → docker 一律报 "cannot connect to the docker API"。
  #   在自己的终端里通常没这问题，保险起见这里显式清掉。
  echo "== 5. 用 docker 起编排栈 =="
  unset DOCKER_HOST
  cd "$BASE/orchestration"
  docker compose up -d --build
  docker compose ps
  exit 0
fi

if [[ "${1:-}" == "--n8n" ]]; then
  echo "== 5. 启动 n8n :5678 =="
  export N8N_SECURE_COOKIE=false
  cd "$BASE"
  nohup /home/qmr/.local/bin/npx --yes n8n >"$BASE/orchestration/n8n.log" 2>&1 &
  echo $! > "$BASE/orchestration/n8n.pid"
  echo "   n8n 启动中，稍后访问 http://127.0.0.1:5678"
fi

cat <<EOF

完成。
  健康检查  curl http://127.0.0.1:8000/health
  资源现状  curl http://127.0.0.1:8000/guard/status
  手动释放  curl -X POST http://127.0.0.1:8000/guard/free
  停止      kill \$(cat $BASE/orchestration/pipeline.pid)
  日志      tail -f $BASE/orchestration/pipeline.log
EOF
