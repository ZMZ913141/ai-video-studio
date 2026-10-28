#!/usr/bin/env bash
# 把 Kinocut + om-comfyui 两个 MCP server 注册到 Claude Code / Cursor 的同一个 agent。
# 跑完之后，agent 同时拥有「生成(ComfyUI)」和「剪辑烧字幕(Kinocut)」两套工具。
set -e

STACK=/home/qmr/github/ai-video-studio
OM_MCP=$STACK/mcp-servers/om-comfyui
OM_WORKFLOWS=/home/qmr/github/ai-video-studio/OpenMontage/tools/_comfyui/workflows

# 1) 装 om-comfyui 的依赖（一次性）
cd "$OM_MCP"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 注册 Kinocut（官方包，经 uvx 自动拉取，无需手动装）
claude mcp add kinocut -- uvx --from kinocut kino

# 3) 注册 om-comfyui（生成层，指向本地 venv）
claude mcp add om-comfyui \
  -e COMFYUI_SERVER_URL=http://localhost:8188 \
  -e COMFYUI_WORKFLOWS_DIR="$OM_WORKFLOWS" \
  -- "$OM_MCP/.venv/bin/python" "$OM_MCP/server.py"

# 4) 校验
claude mcp list
echo "看到 kinocut 和 om-comfyui 即为成功。"
