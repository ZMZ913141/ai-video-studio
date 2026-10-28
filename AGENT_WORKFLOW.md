# Agent 工作流 SOP（MCP 模式）

> 前提：已按 `mcp-config/claude_commands.sh` 把 `kinocut` 和 `om-comfyui` 两个 MCP 注册到
> 同一个 Claude Code / Cursor 会话，且 ComfyUI 在 :8188 跑着（独占 4080）。
> 此时 agent 同时拥有两套工具：
> - **om-comfyui**：`comfyui_generate` / `list_workflows` / `comfyui_health` —— ComfyUI 生成
> - **kinocut**：`trim` / `merge` / `subtitles` / `ai_transcribe` / `resize` / `normalize` /
>   `release_checkpoint` 等 203 个带护栏的工具 + Video Receipt

## 典型任务：一段口播 → 带字幕的竖屏短视频

直接对 agent 说（自然语言即可，不用记命令）：

```
帮我用 ComfyUI 生成一段 5 秒的赛博朋克城市夜景视频（wan22-t2v-4step），
再把这段视频转成 9:16 竖屏、烧上中文字幕、做响度归一化，最后出 Video Receipt 给我。
字幕内容：「夜幕降临，霓虹亮起，城市开始呼吸。」
```

agent 会自动：
1. 调 `comfyui_generate(workflow="wan22-t2v-4step", prompt="...", length=81)` → 拿到 `output_path`
2. （可选）调 `ai_transcribe` 出 SRT，或你直接给字幕文本后用 `subtitles()` 烧录
3. 调 `resize(..., aspect_ratio="9:16")` 转竖屏
4. 调 `normalize(...)` 做响度
5. 调 `release_checkpoint(...)` 出质检 + 缩略图 + Receipt

全程 Preflight 护栏拦截危险参数，结束给你一份可审计的 Video Receipt。

## 任务二：图片编辑 / TTS / 音乐

```
用 flux2 生成一张产品概念图（1024x1024），然后让 Kinocut 把它裁成 1:1 并加描边。
```
（图生视频用 `wan22-i2v-4step` + `reference_image`；音乐用 `ace-step-1-t2a` + `lyrics`。）

## 和 OpenMontage 的关系

OpenMontage 是「agent-first」框架——它的 skills（YAML/Markdown）教 agent 怎么做完整视频管线，
工具层恰好可以走这两个 MCP：生成用 `om-comfyui`，后处理/烧字幕用 `kinocut`。
所以你可以在 OpenMontage 仓库目录里启动 agent，让它既读 OM 的 skills，又用这两个 MCP 工具——

```
cd /home/qmr/github/ai-video-studio/OpenMontage
claude            # 此时 .mcp.json / 已注册的 MCP 自动可用
> 按 cinematic pipeline 给我做一条 30 秒预告片，生成用 ComfyUI，剪辑烧字幕用 Kinocut
```

## 失败排查

- `comfyui_health` 返回不可达 → ComfyUI 没起 / 地址不对（查 `COMFYUI_SERVER_URL`）。
- Kinocut 报错 → 确认本机 `ffmpeg` 在 PATH；`kino doctor` 自检。
- 想换模型 → 把新导出的 API-format workflow JSON 放进 `COMFYUI_WORKFLOWS_DIR`，
  在 `mcp-servers/om-comfyui/comfy_client.py` 的 `WORKFLOW_REGISTRY` 加一条即可。
