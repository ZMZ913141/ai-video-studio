#!/usr/bin/env bash
# ============================================================================
# 一条龙安装 Docker CE（最新稳定版）+ Compose v2 + 可选 NVIDIA 容器工具包
#
# 为什么需要 sudo：dockerd 要写 /var/lib/docker、加载内核 cgroup/iptables 规则、
# 监听 /var/run/docker.sock —— 全部需要 root。Agent 侧 NoNewPrivs=1，提不了权，
# 所以本脚本必须**由你在自己的终端执行**：
#
#     bash /home/qmr/github/ai-video-studio/bin/install-docker.sh
#
# 它会自己 sudo（有则直连 root，无则提示输入密码）。全程幂等，可重复跑。
# ============================================================================
set -euo pipefail

C_RESET='\033[0m'; C_OK='\033[32m'; C_WARN='\033[33m'; C_ERR='\033[31m'; C_HDR='\033[1;36m'
hdr() { printf "\n${C_HDR}== %s ==${C_RESET}\n" "$1"; }
ok()  { printf "  ${C_OK}✓${C_RESET} %s\n" "$1"; }
warn(){ printf "  ${C_WARN}!${C_RESET} %s\n" "$1"; }
err() { printf "  ${C_ERR}✗${C_RESET} %s\n" "$1"; }

# ---- 0. 提权：已经是 root 就不套 sudo --------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
  if ! command -v sudo >/dev/null; then err "没有 sudo"; exit 1; fi
fi
REAL_USER="${SUDO_USER:-$(id -un)}"

hdr "0. 环境"
[ -r /etc/os-release ] || { err "非 Debian/Ubuntu 系，请手动安装"; exit 1; }
. /etc/os-release
CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
ARCH="$(dpkg --print-architecture)"
ok "系统 ${PRETTY_NAME:-$ID} / ${ARCH} / codename=${CODENAME}"

if [ "${ID}" != "ubuntu" ] && [ "${ID}" != "debian" ]; then
  warn "非 Ubuntu/Debian（ID=${ID}）仍会尝试，但仓库地址可能不匹配"
fi

# ---- 1. 卸载冲突的旧包（不删 /var/lib/docker，镜像卷不动）-------------------
hdr "1. 清理冲突旧包"
for p in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg -s "$p" >/dev/null 2>&1; then
    warn "移除 $p"
    $SUDO apt-get remove -y "$p" >/dev/null 2>&1 || true
  fi
done
ok "无冲突包残留"

# ---- 2. 依赖 + 官方仓库 ----------------------------------------------------
hdr "2. 添加 Docker 官方仓库"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq ca-certificates curl gnupg
$SUDO install -m 0755 -d /etc/apt/keyrings
$SUDO curl -fsSL https://download.docker.com/linux/${ID}/gpg \
     -o /etc/apt/keyrings/docker.asc
$SUDO chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${CODENAME} stable" \
  | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
$SUDO apt-get update -qq
ok "仓库已就绪"

# ---- 3. 安装（最新稳定版）--------------------------------------------------
hdr "3. 安装 Docker CE + Compose v2 + Buildx"
$SUDO apt-get install -y \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
ok "安装完成"

# ---- 4. 服务 ---------------------------------------------------------------
hdr "4. 启动服务"
$SUDO systemctl enable --now docker
for i in $(seq 1 30); do
  $SUDO docker info >/dev/null 2>&1 && break
  sleep 1
done
ok "dockerd: $($SUDO systemctl is-active docker)"
ok "版本: $(docker --version 2>/dev/null || $SUDO docker --version)"
ok "Compose: $($SUDO docker compose version 2>&1 | head -1)"

# ---- 5. 免 sudo ------------------------------------------------------------
hdr "5. 把 ${REAL_USER} 加入 docker 组"
if id -nG "$REAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  ok "已在 docker 组"
else
  $SUDO usermod -aG docker "$REAL_USER"
  ok "已加入（**当前终端需重新登录才生效**：也可先跑 newgrp docker）"
fi

# ---- 6. NVIDIA 容器工具包（可选，只有要把 GPU 塞进容器时才需要）------------
hdr "6. NVIDIA 容器工具包（可选）"
if command -v nvidia-smi >/dev/null 2>&1; then
  warn "检测到 NVIDIA 显卡：$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  echo "  本栈默认 ComfyUI 跑在宿主机（独占 GPU），容器里只跑 n8n / pipeline-service，"
  echo "  **不需要**它。只有当你想连 ComfyUI 一起容器化时才装："
  echo
  echo "    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\"
  echo "      | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
  echo "    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\"
  echo "      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\"
  echo "      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list"
  echo "    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit"
  echo "    sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
else
  ok "无 NVIDIA 显卡，跳过"
fi

# ---- 7. 验证 ---------------------------------------------------------------
hdr "7. 拉取 hello-world 验证"
if $SUDO docker run --rm hello-world >/dev/null 2>&1; then
  ok "跑通了"
else
  warn "拉取失败（多为网络/镜像源问题），容器本身已就绪，可稍后重试"
fi

hdr "完成"
cat <<EOF
  常用：
    docker compose up -d          # 注意是空格，不是 docker-compose
    docker compose logs -f
    docker ps

  本栈启动：
    cd /home/qmr/github/ai-video-studio/orchestration
    cp config.env.example .env && docker compose up -d

  ⚠ 重新登录（或 newgrp docker）后才不用 sudo。
EOF
