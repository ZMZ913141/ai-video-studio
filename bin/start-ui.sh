#!/usr/bin/env bash
# ============================================================================
# 一键起本地网页工作台（驾驶舱）
#
#   bash bin/start-ui.sh              # 后台起，打印真实地址
#   bash bin/start-ui.sh --fg         # 前台起，Ctrl+C 停（看得见日志）
#   bash bin/start-ui.sh --open       # 起完顺便开浏览器
#   bash bin/start-ui.sh --status     # 现在的地址是什么
#   bash bin/start-ui.sh --stop       # 停掉上一次起的那个
#   bash bin/start-ui.sh --restart    # 停掉再起
#   bash bin/start-ui.sh --port 9000  # 指定端口（默认 8790，占了自动顺延）
#
# 这个脚本存在的唯一理由：**端口会自动顺延，地址不能靠猜**。
# 8790 撞车是这台机器的常态，真实端口写在小纸条里，脚本负责读它。
# ============================================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# ★ 纸条位置必须跟服务端写的一致（ui_server.py 的 PORTFILE = .cache/ui.port.json）。
#   以前这里读的是 /tmp/vstudio-ui.json —— 服务端早搬走了，脚本还读老地方，
#   于是 --status/--stop 永远看不见已有实例（重启、换终端、/tmp 被清之后尤甚），
#   再点一次启动就又起一个，端口一路顺延，人对着旧页面改半天没反应。
PORTFILE="$BASE/.cache/ui.port.json"
LOGDIR="$BASE/.cache/ui"
LOG="$LOGDIR/server.log"

# ---- 选解释器：UI 服务只用标准库，但要挑一个确定存在的 ----------------------
PY="${PY:-}"
if [ -z "$PY" ]; then
  for c in \
    /home/qmr/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
    /usr/bin/python3 \
    python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "✗ 找不到 python3" >&2; exit 1
fi

# ---- 小工具 ------------------------------------------------------------------
# 端口上跑的是不是这个服务
# 不能只看 HTTP 200 —— 这台机器前面有反代，没监听的端口它回 200 + "upstream
# connect failed"，只看状态码会把空气当成服务。认返回体里的 ok 字段。
is_ours() {
  curl -s -m 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -Eq '"ok": *true'
}

# 是不是"当前这版代码"。新版 health 会自报解释器路径（py 字段），
# 早期版本没有 —— 用它区分"能放心用"和"该清掉的乱码版"。
is_current() {
  curl -s -m 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -q '"py":'
}

# 读小纸条里的端口（小纸条不存在则输出空）
saved_port() {
  [ -f "$PORTFILE" ] || return 0
  "$PY" - "$PORTFILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("port", ""))
except Exception:
    pass
PYEOF
}

# 小纸条里的 pid 还活着吗
saved_alive() {
  [ -f "$PORTFILE" ] || return 1
  local pid
  pid="$("$PY" - "$PORTFILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("pid", ""))
except Exception:
    pass
PYEOF
)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# 找有没有"不受管的"实例占着默认端口段（多半是早期版本，没有小纸条）
find_stray() {
  local p
  for p in 8790 8791 8792 8793 8794 8795; do
    if is_ours "$p"; then echo "$p"; return 0; fi
  done
  return 1
}

# ---- 参数 --------------------------------------------------------------------
MODE="start"; FG=0; OPEN=0; PORT=8790; PORT_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fg)       FG=1 ;;
    --open)     OPEN=1 ;;
    --status)   MODE="status" ;;
    --stop)     MODE="stop" ;;
    --restart)  MODE="restart" ;;
    --port)     shift; PORT="${1:?--port 需要一个数字}"; PORT_SET=1 ;;
    -h|--help)  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 && !/^#/ && NF {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)          echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# ---- stop / status 直接交给服务端自己的实现（它带 PID 验明正身） -------------
if [ "$MODE" = "status" ]; then
  exec "$PY" "$BASE/bin/ui_server.py" --status
fi

if [ "$MODE" = "stop" ]; then
  exec "$PY" "$BASE/bin/ui_server.py" --stop
fi

if [ "$MODE" = "restart" ]; then
  "$PY" "$BASE/bin/ui_server.py" --stop || true
  sleep 1
fi

# ---- 已在跑就别起第二个（用户显式给了别的端口 = 他故意要第二个，别拦） --------
if [ "$PORT_SET" -eq 0 ] && saved_alive; then
  P="$(saved_port)"
  if [ -n "$P" ] && is_ours "$P"; then
    echo "✓ 已经在跑了：http://127.0.0.1:$P"
    echo "  要换掉它：bash bin/start-ui.sh --restart"
    echo "  要停：    bash bin/start-ui.sh --stop"
    [ "$OPEN" -eq 1 ] && { command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:$P" >/dev/null 2>&1 || true; }
    exit 0
  fi
fi

# ---- 没有小纸条但端口有人应答 = 别人起的实例 ----------------------------------
# 只在用默认端口段拦人；用户自己指定了端口，说明他就是要绕开，别多嘴。
if [ "$PORT_SET" -eq 0 ] && STRAY="$(find_stray)"; then
  if is_current "$STRAY"; then
    # 是当前这版代码，只是没走本脚本（所以没留小纸条）。
    # 别杀 —— 人家页面开着，杀了就是白屏重来。直接告诉他地址。
    URL="http://127.0.0.1:$STRAY"
    echo "✓ 已经在跑了：$URL"
    echo "  （这份不是本脚本起的，所以没有小纸条，--stop 管不到它）"
    echo "  要纳入管理：pkill -f 'bin/ui_server.py'; sleep 1; bash bin/start-ui.sh"
    if [ "$OPEN" -eq 1 ] && command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 &
    fi
    exit 0
  fi
  echo "⚠ 端口 $STRAY 上有一份工作台在应答，但它是老版本"
  echo "  （中文路径会乱码）。先试着清掉它……"
  pkill -f 'bin/ui_server.py' 2>/dev/null || true
  sleep 1
  if STRAY2="$(find_stray)"; then
    echo "✗ 自动清理没成功（$STRAY2 还在应答）。两条路："
    echo "    a) 你手动清：  pkill -f 'bin/ui_server.py'   然后重跑本脚本"
    echo "    b) 绕开它：    bash bin/start-ui.sh --port 8800"
    exit 1
  fi
  echo "  ✓ 清掉了"
fi

# ---- 起 ----------------------------------------------------------------------
mkdir -p "$LOGDIR"
: >"$LOG"

if [ "$FG" -eq 1 ]; then
  echo "▸ 前台运行，Ctrl+C 停。日志同时写 $LOG"
  # 不用 exec：exec 放进管道只在左侧子 shell 生效，反而绕。
  "$PY" -u "$BASE/bin/ui_server.py" --port "$PORT" 2>&1 | tee -a "$LOG"
  exit "${PIPESTATUS[0]}"
fi

# </dev/null 不是多余的：后台进程若拿到终端 stdin，读一下就吃 SIGTTIN 停在那儿，
# 表现是"进程在、CPU 0%、没报错、端口不通"，很难查。
nohup "$PY" -u "$BASE/bin/ui_server.py" --port "$PORT" \
  >"$LOG" 2>&1 </dev/null &
LAUNCHED=$!

# ---- 等小纸条，读真实端口 ----------------------------------------------------
URL=""
for _ in $(seq 1 30); do      # 最多 6 秒
  sleep 0.2
  P="$(saved_port)"
  if [ -n "$P" ] && is_ours "$P"; then
    URL="http://127.0.0.1:$P"
    break
  fi
  kill -0 "$LAUNCHED" 2>/dev/null || break
done

if [ -z "$URL" ]; then
  echo "✗ 没起来。日志尾部：" >&2
  tail -n 20 "$LOG" >&2
  exit 1
fi

echo "✓ 工作台已启动  $URL"
echo "  引擎根 $BASE"
[ -d "${STUDIO_ARCHIVE:-/cfu/ai视频档案库}" ] && echo "  档案区 ${STUDIO_ARCHIVE:-/cfu/ai视频档案库}"
echo "  日志   tail -f $LOG"
echo "  停止   bash bin/start-ui.sh --stop"
if [ -f "$PORTFILE" ]; then
  "$PY" - "$PORTFILE" "$PORT" <<'PYEOF' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
want, real = int(sys.argv[2]), int(d.get("port") or 0)
if real and real != want:
    print(f"  （{want} 被占，已顺延到 {real}）")
PYEOF
fi

if [ "$OPEN" -eq 1 ]; then
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  else
    echo "  手动打开：$URL"
  fi
fi
