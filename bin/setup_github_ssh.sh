#!/usr/bin/env bash
# 配 GitHub SSH：生成密钥 → 绕过 22 端口封锁 → 改远程地址 → 输出公钥

set -euo pipefail

KEY="${HOME}/.ssh/id_ed25519"
REPO_DIR="/home/qmr/github/ai-video-studio"

echo "── 1/4 生成密钥（无密码，一路回车即可）──"
if [ -f "$KEY" ]; then
  echo "   已有密钥，跳过：$KEY"
else
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  ssh-keygen -t ed25519 -C "qmr@ai-video-studio" -f "$KEY" -N ""
fi

echo
echo "── 2/4 写 SSH 配置（22 端口被封，走 443）──"
CFG="${HOME}/.ssh/config"
touch "$CFG" && chmod 600 "$CFG"
if ! grep -q "Host github.com" "$CFG" 2>/dev/null; then
  cat >> "$CFG" <<'EOF'

Host github.com
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
EOF
  echo "   已写入 $CFG"
else
  echo "   已有 github.com 配置，跳过"
fi

echo
echo "── 3/4 改远程地址 ──"
cd "$REPO_DIR"
git remote set-url origin git@github.com:ZMZ913141/ai-video-studio.git
git remote -v

echo
echo "════════════════════════════════════════════════"
echo " 4/4 还差最后一步：把下面的公钥加到 GitHub"
echo "     GitHub → Settings → SSH and GPG keys → New SSH key"
echo "     标题随便填，内容整行复制（以 ssh-ed25519 开头）"
echo "════════════════════════════════════════════════"
echo
cat "${KEY}.pub"
echo
echo "加好后回来验证（看到 Hi ZMZ913141 就成功）："
echo "   ssh -T git@github.com"
echo
echo "然后我就能推送："
echo "   cd $REPO_DIR && git push -u origin main"
