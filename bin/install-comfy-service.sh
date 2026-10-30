#!/usr/bin/env bash
# ============================================================
# 一键把 ComfyUI 装成「用户级常驻服务」—— 一次装好，之后不用再管
#
# 装完之后的行为：
#   · 开机自动起（启用 linger 后，不用登录也在）
#   · 进程死了 5 秒后自动重启
#   · 每次死都写清退出码 + 内核日志（Xid / OOM）到 .cache/comfy/supervisor.log
#
# 用法（必须在**你自己的终端**跑，不能由 AI 代跑）：
#   bash bin/install-comfy-service.sh
#   bash bin/install-comfy-service.sh --uninstall
#
# 为什么 AI 代跑不行
# ------------------
# 起 ComfyUI 要能调 CUDA。AI 的执行环境被 seccomp 挡着，torch.cuda.is_available()
# 恒为 False，装出来的服务也只会是个没显卡的空壳。所以这一步必须你来。
# ============================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/comfyui.service"
SRC="$BASE/deploy/comfyui.service"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now comfyui 2>/dev/null
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "✓ 已卸载 comfyui.service（ComfyUI 本体没动）"
  exit 0
fi

echo "== 0/4 环境自检 =="
if ! command -v systemctl >/dev/null; then
  echo "✗ 没有 systemctl，改用守护脚本：bash $BASE/bin/comfy_supervisor.sh start"; exit 1
fi
if ! systemctl --user is-system-running >/dev/null 2>&1; then
  echo "✗ 连不上用户级 systemd（可能不在图形/登录会话里）"
  echo "  退路：bash $BASE/bin/comfy_supervisor.sh start"
  exit 1
fi
if [ ! -x "$BASE/bin/comfy_supervisor.sh" ]; then
  chmod +x "$BASE/bin/comfy_supervisor.sh"
fi
echo "✓ systemd 用户会话可用"

echo "== 1/4 装单元文件 =="
mkdir -p "$UNIT_DIR"
install -m 644 "$SRC" "$UNIT"
echo "✓ $UNIT"

echo "== 2/4 重载并启用 =="
systemctl --user daemon-reload
systemctl --user enable comfyui
echo "✓ 已设为开机自启（default.target）"

echo "== 3/4 现在启动 =="
systemctl --user restart comfyui
sleep 4
systemctl --user --no-pager status comfyui | head -8

echo "== 4/4 开机免登录（可选）=="
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
  echo "✓ linger 已开启，重启后不用登录也会起"
else
  echo "  想让**开机就起、不必登录**，执行一次：sudo loginctl enable-linger $USER"
  echo "  （不执行也行：登录后几秒内自动起）"
fi

cat <<EOF

装好了。常用命令：
  systemctl --user status comfyui       # 活着吗
  systemctl --user restart comfyui      # 立刻重启
  bash $BASE/bin/comfy_supervisor.sh status   # 更详细：重启过几次、上次怎么死的
  tail -f $BASE/.cache/comfy/supervisor.log   # 死因取证日志

★ 改启动参数（比如想回高性能模式）：
  systemctl --user edit comfyui     # 加一行 Environment=COMFY_HIGH_RAM=1
  改完 systemctl --user restart comfyui
EOF
