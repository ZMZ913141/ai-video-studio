#!/usr/bin/env bash
# 配 GitHub SSH：生成密钥 → 绕过 22 端口封锁 → 改远程地址 → 输出公钥
#
# ⚠️ 千万不要用 sudo 跑这个脚本。
#   2026-10-28 就栽过：`sudo ./setup_github_ssh.sh` 把密钥写进了 /root/.ssh，
#   而 git 是以 qmr 身份跑的，读的是 ~/.ssh → 报 "no such identity"，
#   看着像"密钥没生效"，其实是生成到别人家里去了。
#   本脚本不需要任何特权：全程只写 ~/.ssh 和改本仓库的 git remote。

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "✗ 不要用 sudo/root 跑：密钥会生成到 /root/.ssh，而 git 用的是普通用户。" >&2
  echo "  直接执行： bash $0" >&2
  exit 1
fi

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
echo "然后就能推送了："
echo "   cd $REPO_DIR && git push -u origin main"
echo
cat <<'NOTE'
────────────────────────────────────────────────────────────
关于 302 反代（用户 2026-10-28 提到）
  302 反代只处理 HTTP(S)，常见实现只放行 GET/HEAD：
  clone/pull 能过，**push 会 405 / 302 到登录页**，报错通常是
  "The requested URL returned error: 302" 或 "RPC failed"。
  所以推代码走 SSH 443（本脚本配的这条），完全不经过反代。
  → 反代留给下载/拉模型用，git push 别绕它。
  如果哪天换成 HTTPS + PAT 推送，才需要给 git 单独设代理：
      git config --global http.https://github.com.proxy http://127.0.0.1:PORT
  注意 ssh 不读这个配置，设了也不影响 SSH 那条路。
────────────────────────────────────────────────────────────
NOTE
