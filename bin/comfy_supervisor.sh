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
LOCKF="$D/lock"
STATF="$D/state.json"

HEALTH_TIMEOUT="${COMFY_HEALTH_TIMEOUT:-300}"   # 冷启动要加载模型，给足 5 分钟
mkdir -p "$D"

ts() { date '+%m-%d %H:%M:%S'; }
say() { printf '%s %s\n' "$(ts)" "$*"; }

http_ok() {
  # ★ 必须绕代理：本机环境设了 http_proxy，不绕会拿到 502 而误判「服务死了」
  curl -fsS --noproxy '*' -m 3 -o /dev/null "$URL/system_stats" 2>/dev/null
}

is_comfy_proc() {
  # 校验一个 pid 是不是**我们的** ComfyUI：cmdline 里既有 main.py 又有本端口。
  # ★ 2026-10-30 踩过：start_comfy 用 `$!` 记 pid，实测记到了 127608（早就没了的
  #   某个子 shell），真实进程是 126027。后果是 status 永远显示「pid 不可见」，更要命的是
  #   下次重启会 `kill` 一个无关的 pid。所以 kill 之前必须按 cmdline 确认身份。
  local p="$1" c
  [ -n "${p:-}" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  c="$(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null)" || return 1
  case "$c" in
    *main.py*"--port $PORT"*|*main.py*"--port=$PORT"*) return 0 ;;
  esac
  return 1
}

find_comfy_pid() {
  # 从运行中的进程反查，不信任 pid 文件（$! 在小壳里不可靠）
  local p
  for p in $(ps -eo pid,args 2>/dev/null | grep -E "main\.py.*(--port $PORT|--port=$PORT)" | grep -v grep | awk '{print $1}'); do
    is_comfy_proc "$p" && { echo "$p"; return 0; }
  done
  return 1
}

comfy_pid() {
  local p
  if [ -f "$CPIDF" ]; then
    p="$(cat "$CPIDF" 2>/dev/null)"
    is_comfy_proc "${p:-}" && { echo "$p"; return 0; }
  fi
  # pid 文件失效 → 反查，顺便把文件纠正过来
  if p="$(find_comfy_pid)"; then
    echo "$p" >"$CPIDF" 2>/dev/null
    echo "$p"; return 0
  fi
  return 1
}

kill_comfy() {
  # 只杀确认过身份的进程
  local p="$1"
  if is_comfy_proc "${p:-}"; then
    kill "$p" 2>/dev/null
  else
    say "⚠ 拒绝 kill ${p:-空}：它的 cmdline 不是本端口的 ComfyUI（防止误杀）"
  fi
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
    "") say "  ↳ 无退出码（这不是本守护拉起的实例，只能靠内核日志判死因）" ;;
    137) say "  ↳ 退出码 137 = 被 SIGKILL。两种可能：RAM 被内核 OOM 杀掉，或有人 kill -9" ;;
    129) say "  ↳ 退出码 129 = SIGHUP，通常是**启动它的终端被关掉了**" ;;
    143) say "  ↳ 退出码 143 = SIGTERM，被正常终止（有人 stop / 系统注销）" ;;
    127) say "  ↳ 退出码 127 别当结论：经子 shell 时这只是 wait 拿不到真实退出码，不代表「命令找不到」" ;;
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
  rm -f "$CPIDF"
  # ★ 端口已经有人服务着，就别再起一个：抢不到端口的那个会刷
  #   「Port 8188 is already in use」+ 一串假 IMPORT FAILED，纯属自己给自己添乱。
  if http_ok; then
    say "端口已有服务在跑 —— 接管看护，不重复启动"
    return 0
  fi
  if ! why="$(gpu_ok 2>&1)"; then
    say "✗ 当前终端拿不到 GPU（$why）→ 不启动。"
    say "  这不是参数问题：沙箱/seccomp 会挡住 CUDA 驱动调用，起来的只会是个没显卡的空壳。"
    say "  请在**你自己的终端**执行：  bash $BASE/bin/comfy_supervisor.sh start"
    return 3
  fi
  say "启动 ComfyUI：$COMFY_DIR/run.sh"
  # ★ 子进程里先关掉锁 fd（9），否则 ComfyUI 会继承这个 flock ——
  #   守护自己死了，锁却还被 ComfyUI 拿着，下次 start 会误报「守护已在运行」。
  #   2026-10-30 实测踩过：老守护退出后锁仍被占，`start` 直接拒绝。
  ( exec 9>&-; cd "$COMFY_DIR" && exec bash ./run.sh ) >>"$CLOG" 2>&1 &
  echo $! >"$CPIDF"
  local i=0
  while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    if http_ok; then
      # ★ 服务一起来就按 cmdline 反查真实 pid 覆盖掉 $!：
      #   run.sh 之间可能多一层子 shell，$! 未必是跑 main.py 的那个。
      local real; real="$(find_comfy_pid || true)"
      [ -n "${real:-}" ] && echo "$real" >"$CPIDF"
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
  # ★ 单实例锁。2026-10-30 实测：没这把锁时，`start` 连按两次（或两个终端同时敲）
  #   会起两个守护、两个 ComfyUI —— 第二个抢不到 8188 只能报
  #   「Port 8188 is already in use」然后去死，白跑一遍还刷一堆假报错
  #   （用户看到的那条 SeedVR2 IMPORT FAILED 就是它刷的）。
  exec 9>"$LOCKF"
  if ! flock -n 9; then
    say "已有一个守护在跑（锁 $LOCKF 被占），本进程直接退出"
    exit 0
  fi
  trap 'say "守护收到退出信号"; p="$(comfy_pid || true)"; [ -n "${p:-}" ] && kill_comfy "$p"; exit 0' INT TERM
  rm -f "$STOPF"
  # ★ GPU 拿不到就直接退出，**不能进重试循环** ——
  #   那样它会每 5 秒起一个没显卡的空壳，把日志刷满，人也查不出为什么。
  #   ★ 但接管分支不需要 GPU：服务已经活着，本进程只负责看护。
  local why
  if ! http_ok; then
    if ! why="$(gpu_ok 2>&1)"; then
      say "✗ 起不了：$why"
      say "  请在能拿到 GPU 的终端里运行，守护进程会自动接管后续的重启。"
      exit 3
    fi
  fi
  say "守护启动（pid $$）· 目标 $URL · 日志 $LOG"
  local backoff=5 restarts=0 t_start t_end pid="" owned=0 misses=0
  t_start=$(date +%s)
  while [ ! -f "$STOPF" ]; do
    # ---- 服务通：只做心跳，绝不另起一个去抢端口 ----
    if http_ok; then
      if [ "$owned" = "0" ]; then
        say "接管：$URL 上已有实例在跑（不是本守护起的，退出码记不到）"
        owned=1
        # 认到 pid 才算真看护：卡死能杀、能拿到存活时长；认不到就只剩心跳
        pid="$(comfy_pid || true)"
        if [ -n "${pid:-}" ]; then
          say "  已按 cmdline 认到 pid $pid（后续卡死能处理，退出码仍记不到）"
        else
          say "  ⚠ 认不到 pid（cmdline 对不上）—— 只能心跳，卡死时需要人工干预"
        fi
      fi
      misses=0; backoff=5
      sleep 5
      continue
    fi

    # ---- 服务不通 ----
    misses=$((misses + 1))

    # 自己起的进程还活着 → 可能正在加载模型，给一个宽限窗口
    if [ "$owned" = "1" ] && [ -n "$pid" ] && is_comfy_proc "$pid"; then
      if [ "$misses" -le $((HEALTH_TIMEOUT / 5)) ]; then
        sleep 5
        continue
      fi
      say "⚠ 进程在，但 ${HEALTH_TIMEOUT}s 内一直没就绪 —— 判定卡死，重启"
      kill_comfy "$pid"
      sleep 3
    fi

    if [ "$owned" = "1" ] && [ -n "$pid" ]; then
      # ★ 不要指望 wait 一定给得出退出码：多一层子 shell 时 wait 直接返回 127
      #   （那不是「命令找不到」，是「这不是我的直接子进程」）。拿不到就老实说拿不到。
      local rc=""
      wait "$pid" 2>/dev/null && rc=0 || rc=$?
      [ "$rc" = "127" ] && rc=""
      t_end=$(date +%s)
      restarts=$((restarts + 1))
      say "════ ComfyUI 退出（rc=${rc:-取不到}，存活 $((t_end - t_start))s，累计重启 $restarts 次）"
      forensics "$rc"
    elif [ "$owned" = "1" ] && [ "$misses" -ge 2 ]; then
      # 接管来的实例没了：没有退出码可拿
      restarts=$((restarts + 1))
      say "════ 接管中的实例已消失（非本守护启动，拿不到退出码）"
      forensics ""
    fi

    sleep "$backoff"
    backoff=$(( backoff * 2 > 120 ? 120 : backoff * 2 ))
    t_start=$(date +%s)
    start_comfy
    if [ -f "$CPIDF" ]; then pid="$(cat "$CPIDF" 2>/dev/null)"; owned=1; fi
    misses=0
  done
  say "守护退出"
}

cmd_start() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "守护已在运行（pid $(cat "$PIDF")）"; return 0
  fi
  # 锁检查：pid 文件可能过期/丢失，锁才是不说谎的那个（连按两次 start 也只起一个）
  if ! { exec 8>"$LOCKF" && flock -n 8; } 2>/dev/null; then
    echo "守护已在运行（锁被占）——不重复启动"; return 0
  fi
  exec 8>&-
  local why
  # ★ 已经有健康实例在跑时**不需要本终端有 GPU** —— 接管看护而已。
  #   以前这里无条件自检，导致从沙箱/无卡终端连「认领现有服务」都做不到，
  #   明明服务好好的却只能干等用户手动执行。
  if ! http_ok; then
    if ! why="$(gpu_ok 2>&1)"; then
      echo "✗ 当前终端拿不到 GPU（$why）—— 起了也没用。"
      echo "  在你自己的终端跑：bash $BASE/bin/comfy_supervisor.sh start"
      return 3
    fi
  else
    echo "端口 $PORT 上已有健康的 ComfyUI —— 直接接管看护（无需本终端有 GPU）"
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
  elif http_ok; then
    # 服务通、但 pid 不是本守护记的：说明是别处起的（旧守护/你手开的），已在接管中
    cp="运行中（外部启动，pid 不可见）"
  fi
  # ★ grep -c 无匹配时会打印 0 并以 1 退出；以前写成 `|| echo 0` 会输出两行 "0"
  #   → status 里出现「累计重启 : 0 / 0 次」。用 || true 兜住退出码即可。
  n="$(grep -c '════ ComfyUI 退出' "$LOG" 2>/dev/null || true)"; n="${n:-0}"
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
