# ComfyUI + Kdenlive 自动工作流栈（OpenMontage 加持）

> 个人学习用 · 全开源免费（n8n fair-code / OpenMontage AGPLv3 / ComfyUI GPL）
> 架构：n8n 编排 + OpenMontage 工具层 + ComfyUI 生成 + ffmpeg(ASS) 字幕烧录

## 为什么这么搭

- **ComfyUI 独占你的 4080 GPU**，做图/视频/TTS/音乐生成。
- **OpenMontage 当工具库**：它的 `tools/_comfyui/client.py` 和
  `video_compose` 的 `burn_subtitles` 是现成且经过验证的代码，本栈的
  `pipeline-service` 复用其 ComfyUI REST 调用契约，自带 ffmpeg 字幕烧录。
- **n8n 当编排/触发层**：可视化、Webhook/定时触发、可重试、文件流清晰。
- **字幕烧录用 ffmpeg+ASS**（Kdenlive 底层同款），样式全可控且可脚本化；
  Kdenlive 仅保留为「人工精修时间线」的可选环节（`/render-kdenlive`）。

## 资源竞争结论（重要）

| 编码助手跑法 | 与 ComfyUI 抢 GPU？ |
|---|---|
| 云端 LLM（Claude Code / Cursor / Qwen Code + API） | **不抢**，本地只剩 ComfyUI |
| 本地 LLM（Ollama 跑 27B 等） | **抢**，16GB 下无法和 WAN-14B 同驻 |

✅ **推荐：编码助手用云端 API**，ComfyUI 独占 4080，三者「一起部署」零竞争。

## 目录结构

```
comfy-kdenlive-stack/
├── docker-compose.yml        # n8n + pipeline-service
├── config.env.example        # 环境变量样例
├── pipeline-service/         # FastAPI 编排服务（复用 OM 的 ComfyUI 契约）
│   ├── app.py                # /generate /burn /render-kdenlive /health
│   ├── requirements.txt
│   └── Dockerfile
├── n8n-workflow.json         # 导入 n8n 的编排流程
├── subtitles/example.ass     # ASS 字幕样例（字体/颜色/位置可控）
└── workflows/README.md       # 如何换最新模型 workflow
```

## 部署步骤（在你的 4080 机器上）

```bash
# 1) 单独启动 ComfyUI（独占 GPU，:8188）
#    cd /cfu/ComfyUI && ./run.sh   # 或你自己的启动方式

# 2) 启动编排栈
cd comfy-kdenlive-stack
cp config.env.example .env
docker compose up -d        # 起 n8n(:5678) 和 pipeline-service(:8000)

# 3) 校验
curl http://localhost:8000/health     # 应显示 comfyui_reachable:true
```

## 在 n8n 里用

1. 打开 http://localhost:5678 → Workflows → Import from File → 选 `n8n-workflow.json`。
2. 点 Webhook 节点的「Test」或「Production」拿到回调 URL。
3. POST 调用示例：
```bash
curl -X POST http://localhost:5678/webhook/gen \
  -H 'content-type: application/json' \
  -d '{
    "workflow": "wan22-t2v-4step",
    "prompt": "a cyberpunk city at night, neon rain",
    "width": 832, "height": 480, "length": 81,
    "subtitle_path": "/files/subtitles/example.ass",
    "font_size": 72
  }'
```
返回 `output_path` 即是「生成 + 字幕烧录」后的成片。

## 想直接学 OpenMontage 本体？

在 Claude Code / Cursor 里 `cd` 进 `/home/qmr/github/ai-video-studio/OpenMontage`，
设好 `COMFYUI_SERVER_URL`，用自然语言描述片子即可——它的 agent 会自己驱动
ComfyUI 生成 + ffmpeg 烧字幕，这是 OM 设计的正路。本栈是把它「封装成可触发服务」的版本。
