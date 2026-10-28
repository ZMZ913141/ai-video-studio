#!/usr/bin/env bash
# ============================================================
# ComfyUI 内存护栏 —— 「宁可卡慢，不要被杀」
#
# 用法：
#   ./gpu_guard.sh status          看当前状态（默认）
#   ./gpu_guard.sh tune            诊断当前进程为什么会被杀
#   ./gpu_guard.sh start [模式]     按推荐参数重启 ComfyUI
#                                   safe（默认）：限住缓存 + 护栏，防 OOM
#                                   fast：原样启动，速度优先
#   ./gpu_guard.sh swap-add 32     加 swap（需 root，打印命令给你执行）
#
# ============================================================
set -uo pipefail

COMFY_DIR=/cfu/ComfyUI

# ---- 参数（按 61.5G 物理内存 + RTX 4080S 16G 标定）----
# MemoryHigh 50G → 超了**限速**（疯狂换页/回收），进程活着但变慢
# MemoryMax  58G → 硬顶，比物理 61.5G 留 3.5G 给桌面/系统
MEM_HIGH=50G
MEM_MAX=58G
SWAP_ADD_GB=32

c_hdr() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
c_ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
c_warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }
c_err() { printf '  \033[31m✗\033[0m %s\n' "$1"; }

mem_g() {
  awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} /^SwapTotal/{st=$2} /^SwapFree/{sf=$2}
       END{printf "%.1f %.1f %.1f %.1f", t/1048576, a/1048576, st/1048576, sf/1048576}' /proc/meminfo
}

comfy_pid() { pgrep -f "^/cfu/ComfyUI/.venv/bin/python main\.py" | head -1; }

# ---------------------------------------------------------------- status
do_status() {
  c_hdr "ComfyUI 进程"
  local pid; pid=$(comfy_pid)
  if [ -z "$pid" ]; then
    c_warn "没在跑"
  else
    local rss adj cg
    rss=$(awk '/^VmRSS/{printf "%.1f", $2/1048576}' /proc/$pid/status 2>/dev/null)
    adj=$(cat /proc/$pid/oom_score_adj 2>/dev/null)
    cg="/sys/fs/cgroup$(cat /proc/$pid/cgroup | cut -d: -f3)"
    c_ok "PID $pid  RSS ${rss}G  oom_score_adj=$adj"
    printf '  memory.high = %s   memory.max = %s\n' \
      "$(cat $cg/memory.high 2>/dev/null || echo '?')" \
      "$(cat $cg/memory.max 2>/dev/null || echo '?')"
    [ -f "$cg/memory.peak" ] && printf '  memory.peak = %.1fG（历史峰值）\n' \
      "$(awk -v v=$(cat $cg/memory.peak) 'BEGIN{print v/1073741824}')"
  fi

  c_hdr "内存"
  read -r T A ST SF <<<"$(mem_g)"
  printf '  物理 %.1fG  可用 %.1fG\n' "$T" "$A"
  printf '  swap %.1fG  剩 %.1fG' "$ST" "$SF"
  awk -v sf="$SF" 'BEGIN{ if (sf<2) print "  ← 快满了，OOM 就在眼前"; else print "" }'

  c_hdr "GPU"
  if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null |
      awk -F', ' '{printf "  显存 %s / %s   GPU util %s\n", $1, $2, $3}'
  fi

  c_hdr "内核 OOM 记录"
  local n
  n=$(journalctl --since "7 days ago" 2>/dev/null | grep -c "Out of memory: Killed")
  if [ "${n:-0}" -gt 0 ]; then
    c_err "近 7 天被杀 $n 次（被杀瞬间 swap 都已写满）："
    journalctl --since "7 days ago" 2>/dev/null | grep -E "Out of memory: Killed|Free swap" |
      sed 's/.*qmr-cjb kernel: //' | sed 's/^/    /' | cut -c1-95
  else
    c_ok "无"
  fi
}

# ---------------------------------------------------------------- tune（诊断）
do_tune() {
  local pid; pid=$(comfy_pid)
  if [ -z "$pid" ]; then c_err "ComfyUI 没在跑"; return 1; fi

  c_hdr "当前状态"
  printf '  argv          %s\n' "$(tr '\0' ' ' < /proc/$pid/cmdline)"
  printf '  oom_score_adj %s\n' "$(cat /proc/$pid/oom_score_adj)"
  printf '  RSS           %sG\n' "$(awk '/^VmRSS/{print int($2/1048576)}' /proc/$pid/status)"
  local cg="/sys/fs/cgroup$(cat /proc/$pid/cgroup | cut -d: -f3)"
  printf '  memory.peak   %.1fG\n' "$(awk -v v=$(cat $cg/memory.peak 2>/dev/null || echo 0) 'BEGIN{print v/1073741824}')"
  printf '  VmLck/VmPin   %s / %s KB（页锁定内存，不可换出）\n' \
    "$(awk '/^VmLck/{print $2}' /proc/$pid/status)" \
    "$(awk '/^VmPin/{print $2}' /proc/$pid/status)"

  c_hdr "三个原因（按贡献排序，都有实测依据）"
  cat <<'EOF'
① 【主因】总内存需求紧贴物理容量，swap 又只有 8G
   上一次被杀的瞬间，内核日志原文：
     Node 0 active_anon   19.9G  +  inactive_anon  38.1G  = 58.1G（都压在物理内存里）
     Free swap = 152kB          ← 8G swap 已经全部写满
     Node 0 Normal free = 125MB ← 低于 min 水位 128MB
     all_unreclaimable? yes     ← 内核已回收不出任何东西
   → anon 总需求 ≈ 58.1G（RAM）+ 8G（swap）= 66.1G
   → 容量 = 61.5G + 8G = 69.5G，只剩 3.4G 给内核 slab / 页表 / 桌面
   → 而且 anon **不可回收**，只能靠 swap；swap 一满就必然 OOM

② 【帮凶】缓存阈值默认 = 100% 物理内存，等于「永远清不干净」
   main.py:343        cache_ram_inactive = min(128, 61.5) = 61.5G
   caching.py:552     if virtual_memory_available() >= target: return 0
   目标 61.5G 恰好等于物理总量 → 可用内存永远达不到，每轮都清一遍缓存、
   下一轮又重算。既不省内存也不省时间。

③ 【不是问题】显存只用 4~8G
   ComfyUI 0.42 默认开 DynamicVRAM（没给 --highvram / --disable-dynamic-vram 就开）。
   权重留在主机内存，每步只把当前需要的分块送进 16G 显存 ——
   H3 20G + 文本编码器 15G = 35G 装不进 16G，只能这样流式送。
   这是**设计行为**，不是故障；代价是比「权重常驻显存」慢一些。
EOF

  c_hdr "结论：先加 swap，这是唯一能立刻见效的一步"
  c_ok "swap 8G → 40G：容量 69.5G → 101.5G，对 66G 需求留出 35G 余量"
  c_ok "  → 真正变成「内存紧张只是变慢」，正如 Windows 的 pagefile"
  c_ok "附带 --cache-ram 4 8：把缓存阈值从 61.5G 降到 8G，减少反复清/重算"
  c_ok "附带 --disable-pinned-memory：万一 pin 了内存也能被换页（保险）"
  c_warn "如果还想恢复「吃满显存」：加 --highvram，但 35G 权重装不进 16G，"
  c_warn "  实际只会变成频繁换进换出，更慢。不建议。"
  c_warn "运行中改不了 cgroup 限额（与你的终端 shell 共用 ptyxis scope），必须重启"
}

# ---------------------------------------------------------------- start
do_start() {
  local mode="${1:-safe}"
  local pid; pid=$(comfy_pid)
  if [ -n "$pid" ]; then
    c_err "ComfyUI 已在运行（PID $pid）。先关掉再 start"
    return 1
  fi

  read -r T A ST SF <<<"$(mem_g)"
  c_hdr "启动（模式：$mode）"
  printf '  MemoryHigh=%s  MemoryMax=%s\n' "$MEM_HIGH" "$MEM_MAX"
  printf '  启动前：物理 %.1fG 可用 %.1fG / swap 剩 %.1fG\n' "$T" "$A" "$SF"
  if awk -v sf="$SF" 'BEGIN{exit !(sf<4)}'; then
    c_warn "swap 只剩 ${SF}G。强烈建议先跑：sudo ./gpu_guard.sh swap-add $SWAP_ADD_GB"
  fi

  case "$mode" in
    safe)
      # ★ 三个改动，每个都对着一个已实测的机制：
      #
      # ① --disable-pinned-memory
      #    让权重走普通匿名内存（可换出），而不是 cudaHostRegister 锁页
      #    → 内存压力下内核能把它们换到 swap，表现为「变慢」而不是「被杀」
      #    → 这是最接近 Windows pagefile 行为的一项
      #
      # ② --cache-ram 4 8
      #    active 阈值 4G（默认 6.2G）、inactive 阈值 8G（默认 61.5G）
      #    → 缓存淘汰在「可用内存 < 8G」时才真正开始清，
      #      而不是按 61.5G 那个荒谬的阈值每轮狂清
      #    → 代价：节点结果留得少，重复节点要重算。本工作流是单向流水线，
      #      几乎没有可复用节点，所以完全没损失
      #
      # ③ --reserve-vram 0.5 + cgroup MemoryHigh/Max
      #    显存与内存双护栏
      local EXTRA="--disable-pinned-memory --cache-ram 4 8"
      ;;
    fast)
      local EXTRA=""
      c_warn "fast 模式：pinned memory + 默认缓存阈值，内存不可换出，可能卡死"
      ;;
    *) c_err "未知模式：$mode（可选 safe / fast）"; return 2 ;;
  esac

  cd "$COMFY_DIR" || return 1
  printf '\n  执行：.venv/bin/python main.py %s\n\n' "$EXTRA"

  # ⚠️ 不跑 ./run.sh：它硬写了 --high-ram，而 --high-ram 会让
  #    ensure_pin_budget() 直接 return True（跳过全部内存预算检查），
  #    和 safe 模式目的正好相反。
  # ⚠️ 用户级 systemd-run 不支持 -p OOMScoreAdjust（实测 Unknown assignment）。
  exec systemd-run --user --scope \
    --unit=comfyui-guarded \
    --slice=comfyui.slice \
    -p MemoryHigh="$MEM_HIGH" \
    -p MemoryMax="$MEM_MAX" \
    -- .venv/bin/python main.py \
       --listen 127.0.0.1 --port 8188 \
       --reserve-vram 0.5 \
       $EXTRA "$@"
}

# ---------------------------------------------------------------- swap-add
# ★ 「Windows 那样不被杀」的第一优先级：
#   Windows 的 pagefile 会**自动增长**；Linux 的 swap 是**固定大小**，
#   写满就没了，内核只能杀人。
#   实测两次被杀时 Free swap = 0kB / 152kB —— 就是写满了。
do_swap_add() {
  local gb="${1:-$SWAP_ADD_GB}"
  local f=/swapfile.extra
  c_hdr "扩容 swap（+${gb}G）"
  printf '  当前：\n'; swapon --show | sed 's/^/    /'

  printf '\n需要 root，请复制执行（当前会话无 sudo 权限）：\n\n'
  cat <<EOF
# ---------- 复制以下整段 ----------
# 1) 加 swap 文件（放 ext4 根分区，比 ntfs3 的 /cfu 更适合当 swap）
sudo fallocate -l ${gb}G $f
sudo chmod 600 $f
sudo mkswap $f
sudo swapon -p 10 $f

# 2) 开机自动挂（pri=10 高于 /swap.img 的 -1）
echo '$f none swap sw,pri=10 0 0' | sudo tee -a /etc/fstab

# 3) 让内核更愿意换页（60 → 80，贴合「宁慢不杀」）
echo 'vm.swappiness=80' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl -w vm.swappiness=80

# 4) 放宽 systemd-oomd 的提前抢杀
#    （它是「还没真没内存就先杀」，Windows 没有对应机制）
#    注：oomd 的 SwapUsedLimit=100% 表示「swap 用满才动手」，
#        DefaultMemoryPressureLimit=95% 表示「内存压力 95% 才动手」，
#        默认是 90% / 60%，容易在还没真缺内存时就杀进程。
sudo mkdir -p /etc/systemd/oomd.conf.d
cat <<'OOMD' | sudo tee /etc/systemd/oomd.conf.d/99-relax.conf
[OOM]
SwapUsedLimit=100%
DefaultMemoryPressureLimit=95%
OOMD
sudo systemctl restart systemd-oomd

# 5) 验证
swapon --show && free -g
# ---------- 完 ----------
EOF
}

case "${1:-status}" in
  start)   shift; do_start "$@" ;;
  tune)    do_tune ;;
  swap-add) shift; do_swap_add "$@" ;;
  status|"") do_status ;;
  *) echo "用法: $0 {status|start [safe|fast]|tune|swap-add [GB]}"; exit 2 ;;
esac
