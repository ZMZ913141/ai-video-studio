#!/bin/bash
# 内存扩容：把 swap 从 8G 扩到 80G（默认）。让「内存紧张只是变慢」变成现实，而不是被内核杀掉。
#
# 用法（在**你自己的终端**里跑，不要在 WorkBuddy 里跑）：
#     sudo bash /cfu/洁斌资产/陈洁斌主号/03_生产脚本/swap_expand.sh
#     sudo bash /cfu/洁斌资产/陈洁斌主号/03_生产脚本/swap_expand.sh 64   # 想要别的总容量
#
# ------------------------------------------------------------------
# 为什么必须做这件事（2026-10-27 内核日志实测）
# ------------------------------------------------------------------
#   active_anon 19.9G + inactive_anon 38.1G = 58.1G   ← 全压在物理内存
#   Free swap = 152kB                                ← 8G swap 已写满
#   all_unreclaimable? yes                           ← 什么都回收不出来
# → 需求 66.1G vs 容量 69.5G，只剩 3.4G 给内核和桌面 → 必然 OOM kill
#
# ⚠️ 「swap 写满」不等于「swap 没用上」，恰恰说明换页一直在拼命工作。
#    真问题是**总量不够**。
#
# ------------------------------------------------------------------
# 40G 够不够？80G 会不会太多？（2026-10-28 结论）
# ------------------------------------------------------------------
#   物理内存 64G，实测 anon 峰值 58.1G。
#   40G：总量 104G，对 58.1G 的峰值有 46G 余量 —— **够用**。
#   80G：总量 144G，是峰值的 2.5 倍 —— **更稳，且代价只有磁盘空间**。
#
#   选 80G 的理由：anon 峰值不是固定值，它会随「跑了几小时、缓存累积多少」往上走，
#   58.1G 是那一次被杀时的快照，不是上限。swap 在 NVMe 上，多出来的容量平时
#   根本不会被用到，只有真到生死关头才动 —— 属于「便宜的保险」。
#
#   80G 唯一的副作用：系统会在内存压力下换出更多冷页，桌面可能更卡。
#   但你要的就是「卡而不被杀」，所以这个副作用正是目的。
#   真失控还有 systemd-oomd 兜底（下面已放宽到压力 95% 才动手）。
# ------------------------------------------------------------------
set -euo pipefail

TOTAL="${1:-80}"          # 目标总 swap（G）
SWAPFILE=/swapfile.extra

# ⚠ 别用 `free -g | awk '/^Swap:/'` 取当前容量：中文 locale 下那行是「交换：」，
#   正则匹配不到 → 算成 0，会白建一份重复的 swap。/proc/meminfo 与语言无关。
CUR=$(awk '/^SwapTotal:/{printf "%d", $2/1048576}' /proc/meminfo)
ADD=$((TOTAL - CUR))
if [ "$ADD" -le 0 ]; then
  echo "✓ 当前 swap 已有 ${CUR}G ≥ 目标 ${TOTAL}G，不用动"
  swapon --show
  exit 0
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " 内存扩容：${CUR}G → ${TOTAL}G（新增 ${ADD}G）"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# --- 前置检查 ---
if [ "$(id -u)" -ne 0 ]; then
  echo "✗ 必须用 sudo 跑：sudo bash $0" >&2
  exit 1
fi

ROOT_FS=$(df -T / | awk 'NR==2{print $2}')
ROOT_AVAIL=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
echo " 根分区: $ROOT_FS   可用: ${ROOT_AVAIL}G"

if [ "$ROOT_FS" != "ext4" ] && [ "$ROOT_FS" != "xfs" ] && [ "$ROOT_FS" != "btrfs" ]; then
  echo "⚠ 根分区不是 ext4/xfs/btrfs，swap 文件可能不工作"
  echo "  （ntfs3 不能放 swap 文件，这也是为什么不放 /cfu）"
fi

if [ "$ROOT_AVAIL" -lt "$ADD" ]; then
  echo "✗ 根分区只剩 ${ROOT_AVAIL}G，放不下 ${ADD}G"
  exit 1
fi

if swapon --show | grep -q "$SWAPFILE"; then
  echo "✓ $SWAPFILE 已经在用，跳过创建"
else
  echo
  echo "[1/4] 建 ${ADD}G swap 文件（fallocate，秒级，不写零）"
  fallocate -l "${ADD}G" "$SWAPFILE" 2>/dev/null || {
    echo "  fallocate 失败（可能是 btrfs/老内核），退回 dd（会慢一点）"
    dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((ADD * 1024)) status=progress
  }
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE" >/dev/null
  # pri=10 高于现有 /swap.img 的 -1 → 新的先用，旧的兜底
  swapon -p 10 "$SWAPFILE"
  echo "  ✓ 已启用"
fi

echo
echo "[2/4] 写进 fstab（开机自动挂）"
if ! grep -q "$SWAPFILE" /etc/fstab; then
  echo "$SWAPFILE none swap sw,pri=10 0 0" >>/etc/fstab
  echo "  ✓ 已写入"
else
  echo "  已有，跳过"
fi

echo
echo "[3/4] swappiness $(cat /proc/sys/vm/swappiness) → 80（更愿意换页 = 宁慢不杀）"
# 60 是桌面默认值，偏向「保住内存里的热数据、不换页」。
# 我们要的是相反：宁可让 ComfyUI 的冷权重去 swap，也要保住进程不被杀。
echo 'vm.swappiness=80' >/etc/sysctl.d/99-swap.conf
sysctl -w vm.swappiness=80 >/dev/null
echo "  ✓ 当前 swappiness = $(cat /proc/sys/vm/swappiness)"

echo
echo "[4/4] 放宽 systemd-oomd 的提前抢杀"
# oomd 会在「还没真没内存」时就按压力值抢先杀进程 —— Windows 没有这个机制。
# 默认：内存压力 60% 就动手；swap 用满前就动手。
# 改成：压力 95% 才动手、swap 用满才动手。
if systemctl is-active systemd-oomd >/dev/null 2>&1; then
  mkdir -p /etc/systemd/oomd.conf.d
  cat >/etc/systemd/oomd.conf.d/99-relax.conf <<'OOMD'
[OOM]
SwapUsedLimit=100%
DefaultMemoryPressureLimit=95%
OOMD
  systemctl restart systemd-oomd 2>/dev/null && echo "  ✓ oomd 已放宽"
else
  echo "  systemd-oomd 未运行，跳过"
fi

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " 完成。当前 swap："
swapon --show
free -h | sed -n '1,3p'
echo
echo " 验证：swapon --show 里应该看到 8G + ${ADD}G 两行，Swap 总计 ${TOTAL}G"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
