#!/usr/bin/env bash
# ============================================================
# ComfyUI 守护进程 —— 「挂了就自动拉起，并且留下死因」
#
# 为什么需要它
# ------------
# 2026-10-30 05:24 ComfyUI 在「出完视频、换 TTS 模型」那一刻被 GPU 判死
# （内核 NVRM: Xid 43），进程当场消失、没有 Traceback、日志断在半句。
# 流水线侧的轮询只能**报错退出**，它没法把服务变回来 —— 于是整条片子
# 卡在原地，人也不知道在等什么。
# 这个脚本补的就是那一层：进程死了 5 秒后自动重启，并把死因写进日志。
#
# 用法
# ----
#   bin/comfy_supervisor.sh start     后台常驻（关掉终端也不死）
#   bin/comfy_supervisor.sh status    看：守护在不在 / 服务活着吗 / 重启过几次 / 上次怎么死的
#   bin/comfy_supervisor.sh restart   立刻重启 ComfyUI（守护会接着看住）
#   bin/comfy_supervisor.sh stop      停守护 + 停服务
#   bin/comfy_supervisor.sh logs      跟日志
#
# 它**不能**做什么
# ----------------
# 起 ComfyUI 需要能拿到 GPU。沙箱/无驱动环境里 torch.cuda.is_available() 是 False，
# 强行起也只会得到一个没有显卡的假服务（提交任务必失败，比不起更误事）。
# 所以启动前会先自检 GPU，拿不到就直说，而不是硬起一个残废进程。
# ============================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFY_DIR="${COMFY_DIR:-/cfu/ComfyUI}"
PORT="${COMFY_PORT:-8188}"
URL="http://127.0.0.1:${PORT}"

D="$BASE/.cache/comfy"
LOG="$D/supervisor.log"
CLOG="$D/comfyui.log"
PIDF="$D/supervisor.pid"
CPIDF="$D/comfy.pid"
STOPF="$D/stop"
STATF="$D/state.json"

HEALTH_TIMEOUT="${COMFY_HEALTH_TIMEOUT:-300}"   # 冷启动要加载模型，给足 5 分钟
mkdir -p "$D"

ts() { date '+%m-%d %H:%M:%S'; }
say() { printf '%s %s\n' "$(ts)" "$*"; }

http_ok() {
  # ★ 必须绕代理：本机环境设了 http_proxy，不绕会拿到 502 而误判「服务死了」
  curl -fsS --noproxy '*' -m 3 -o /dev/null "$URL/system_stats" 2>/dev/null
}

comfy_pid() {
  [ -f "$CPIDF" ] || return 1
  local p; p="$(cat "$CPIDF" 2>/dev/null)"
  [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && { echo "$p"; return 0; }
  return 1
}

gpu_ok() {
  local py="$COMFY_DIR/.venv/bin/python"
  [ -x "$py" ] || { echo "找不到 $py"; return 1; }
  local out
  out="$(timeout 120 "$py" -c 'import torch;print(1 if torch.cuda.is_available() else 0)' 2>&1 | tail -1)"
  [ "$out" = "1" ] && return 0
  echo "torch.cuda.is_available() = False"
  return 1
}

# 死因取证：优先内核日志（Xid / OOM），这是应用层看不到的
forensics() {
  local sig="$1" since="-5min"
  case "$sig" in
    137) say "  ↳ 退出码 137 = 被 SIGKILL。两种可能：RAM 被内核 OOM 杀掉，或有人 kill -9" ;;
    129) say "  ↳ 退出码 129 = SIGHUP，通常是**启动它的终端被关掉了**" ;;
    143) say "  ↳ 退出码 143 = SIGTERM，被正常终止（有人 stop / 系统注销）" ;;
    0)   say "  ↳ 退出码 0 = 自己退出的，正常不该发生" ;;
    *)   say "  ↳ 退出码 $sig" ;;
  esac
  local k
  k="$(journalctl -k --since "$since" --no-pager 2>/dev/null \
        | grep -i -E "Xid|out of memory|Killed process|NVRM" | tail -6)"
  if [ -n "$k" ]; then
    say "  ↳ 内核日志（死因在这里，应用日志永远看不到）："
    printf '%s\n' "$k" | sed 's/^/      /'
    case "$k" in
      *"Xid"*)   say "  ↳ 诊断：GPU 层故障（Xid）。最常见是**换模型瞬间**显存/上下文顶到边。" ;;
      *"Killed process"*) say "  ↳ 诊断：内核 OOM。先看 comfy_guard.py oom，再考虑降并发/减模型。" ;;
    esac
  else
    say "  ↳ 内核日志无记录：不是 OOM、不是 GPU 故障 → 大概率是终端关闭或外部 kill"
  fi
  say "  ↳ 该任务需要重跑：vstudio pipeline <IP> <套次> --from <失败的那一步>"
}

start_comfy() {
  local why
  if ! why="$(gpu_ok 2>&1)"; then
    say "✗ 当前终端拿不到 GPU（$why）→ 不启动。"
    say "  这不是参数问题：沙箱/seccomp 会挡住 CUDA 驱动调用，起来的只会是个没显卡的空壳。"
    say "  请在**你自己的终端**执行：  bash $BASE/bin/comfy_supervisor.sh start"
    return 3
  fi
  say "启动 ComfyUI：$COMFY_DIR/run.sh"
  ( cd "$COMFY_DIR" && exec bash ./run.sh ) >>"$CLOG" 2>&1 &
  echo $! >"$CPIDF"
  local i=0
  while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    if http_ok; then
      say "✓ 服务就绪 ${URL}（用时 ${i}s，pid $(cat "$CPIDF")）"
      return 0
    fi
    # 进程都死了就别再等满 5 分钟
    comfy_pid >/dev/null || { say "✗ 进程在启动阶段就退出了"; return 1; }
    sleep 2; i=$((i + 2))
    [ $((i % 30)) -eq 0 ] && say "  …仍在加载（${i}s）"
  done
  say "✗ ${HEALTH_TIMEOUT}s 内没等到就绪（可能在加载超大工作流），继续观察"
  return 1
}

cmd_run() {
  trap 'say "守护收到退出信号"; [ -f "$CPIDF" ] && kill "$(cat "$CPIDF")" 2>/dev/null; exit 0' INT TERM
  rm -f "$STOPF"
  # ★ GPU 拿不到就直接退出，**不能进重试循环** ——
  #   那样它会每 5 秒起一个没显卡的空壳，把日志刷满，人也查不出为什么。
  local why
  if ! why="$(gpu_ok 2>&1)"; then
    say "✗ 起不了：$why"
    say "  请在能拿到 GPU 的终端里运行，守护进程会自动接管后续的重启。"
    exit 3
  fi
  say "守护启动（pid $$）· 目标 $URL · 日志 $LOG"
  local backoff=5 restarts=0 t_start t_end
  while [ ! -f "$STOPF" ]; do
    t_start=$(date +%s)
    start_comfy
    if [ $? -ne 0 ] && ! comfy_pid >/dev/null; then
      say "启动失败，${backoff}s 后重试（连续失败 $((restarts + 1)) 次）"
      sleep "$backoff"
      backoff=$(( backoff * 2 > 120 ? 120 : backoff * 2 ))
      restarts=$((restarts + 1))
      continue
    fi
    backoff=5
    # 守着，直到进程自己没了
    local pid; pid="$(cat "$CPIDF" 2>/dev/null)"
    while kill -0 "$pid" 2>/dev/null; do
      sleep 5
      [ -f "$STOPF" ] && break
    done
    [ -f "$STOPF" ] && break
    wait "$pid" 2>/dev/null; local rc=$?
    t_end=$(date +%s)
    restarts=$((restarts + 1))
    say "════ ComfyUI 退出（rc=$rc，存活 $((t_end - t_start))s，累计重启 $restarts 次）"
    forensics "$rc"
    sleep "$backoff"
  done
  say "守护退出"
}

cmd_start() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "守护已在运行（pid $(cat "$PIDF")）"; return 0
  fi
  local why
  if ! why="$(gpu_ok 2>&1)"; then
    echo "✗ 当前终端拿不到 GPU（$why）—— 起了也没用。"
    echo "  在你自己的终端跑：bash $BASE/bin/comfy_supervisor.sh start"
    return 3
  fi
  setsid nohup bash "$0" run >>"$LOG" 2>&1 &
  local p=$!
  echo "$p" >"$PIDF"
  sleep 3
  if kill -0 "$p" 2>/dev/null; then
    echo "✓ 守护已启动（pid $p），日志：$LOG"
    echo "  服务就绪后用 status 看：bash $0 status"
  else
    echo "✗ 守护没起来，看日志：$LOG"; tail -12 "$LOG"; return 1
  fi
}

cmd_stop() {
  touch "$STOPF"
  [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
  if p="$(comfy_pid)"; then
    kill "$p" 2>/dev/null; sleep 3; kill -9 "$p" 2>/dev/null
  fi
  sleep 1
  echo "已停止"; rm -f "$PIDF" "$CPIDF"
}

cmd_status() {
  local sup="未运行" cp="未运行" up="—" n=0
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    sup="运行中 (pid $(cat "$PIDF"))"
  fi
  if p="$(comfy_pid)"; then
    cp="运行中 (pid $p)"
    # 进程存活时长（etimes 秒）
    local et; et="$(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -n "${et:-}" ] && up="$((et / 60))分$((et % 60))秒"
  fi
  n="$(grep -c '════ ComfyUI 退出' "$LOG" 2>/dev/null || echo 0)"
  local healthy="✗ 不通"
  http_ok && healthy="✓ 通"
  printf '守护进程 : %s\n' "$sup"
  printf 'ComfyUI  : %s，已运行 %s\n' "$cp" "$up"
  printf '健康检查 : %s（%s/system_stats）\n' "$healthy" "$URL"
  printf '累计重启 : %s 次\n' "$n"
  if [ "$n" != "0" ]; then
    printf '上次死因 :\n'
    grep -A6 '════ ComfyUI 退出' "$LOG" 2>/dev/null | tail -7 | sed 's/^/  /'
  fi
}

cmd_logs() { tail -n 60 -f "$CLOG"; }

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 2; cmd_start ;;
  status)  cmd_status ;;
  run)     cmd_run ;;
  logs)    cmd_logs ;;
  *) echo "用法: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
