# ai-video-studio

> 全开源免费 · 视频生产**引擎 + 档案卡**统一工作区
> **ComfyUI 不在此目录**（它在 `/cfu/ComfyUI`，独占 4080 GPU；本工作区只"调用"它）

## ★ 引擎与档案分家（2026-10-28 重构）

```
ai-video-studio/
├── registry.json   ★ 总册：谁有档案卡
├── profiles/       ★ 档案卡：一人一张，互不污染
│   └── 陈洁斌主号/
│       ├── card.json    业务层：需求/习惯/红线/数据策略
│       ├── ip.json      技术层：音色/静帧/采样参数
│       ├── 01~08/       人设/真值/脚本/验收/文案/音频/成片/过程
│       └── 资产/        交付件（本 IP 私有）
├── bin/            vstudio.py（CLI）+ mcp_server.py（MCP）+ llm_router.py
└── orchestration/ captions/ config/ docs/ …   引擎其余部分
```

| 改什么 | 动哪里 | 影响面 |
|---|---|---|
| 引擎升级（模型/字幕/路由） | `bin/` `orchestration/` `captions/` `config/` | 所有 IP 受益，档案卡一字不改 |
| 个人档案（音色/习惯/红线） | `profiles/<IP>/` | 只影响这个 IP，引擎不动 |

**入口三个，同一批底层函数**：CLI（人 + agent）、MCP（别的 AI）、HTTP（n8n）。
UI 不是必需品，将来只做数据显示。→ 详见 `RUNBOOK.md`

```bash
python3 bin/vstudio.py cards      # 看所有档案卡
python3 bin/vstudio.py status     # 总览 + 服务健康
python3 bin/vstudio.py guard 洁斌  # 开工自检
```

## 架构

```
        ┌─────────────────────────────────────────────┐
        │  一个 agent（Claude Code / Cursor）          │
        │  ├─ OpenMontage skills（管线大脑）           │
        │  ├─ om-comfyui MCP  → 生成                  │
        │  └─ kinocut MCP     → 剪辑/烧字幕/质检      │
        └────────────┬────────────────┬───────────────┘
                     │                │
              ComfyUI(:8188)    Kinocut(本地 ffmpeg)
              独占 4080 GPU
                     │
        n8n(:5678) ──┴── pipeline-service(:8000) ──→ 成片
        （Webhook/定时触发，无 LLM 也能跑）
```

- **生成**：ComfyUI（FLUX2 / WAN2.2 / ACE-Step，换模型只丢 JSON）
- **后处理**：Kinocut（带护栏 + Video Receipt）+ 剪映级字幕模块
- **编排**：n8n（可视化、可触发可重试）
- **大脑**：OpenMontage skills（可选，自然语言驱动）
- **已排除**：Kdenlive（手工 NLE，自动化链路不需要）

## 目录

```
ai-video-studio/
├── OpenMontage/            # 迁移进来（.git 完整保留）
├── orchestration/          # n8n + pipeline-service
│   ├── docker-compose.yml
│   ├── pipeline-service/   # /generate /burn（ffmpeg+ASS）
│   ├── n8n-workflow.json
│   ├── mcp-config/
│   └── subtitles/
├── mcp-servers/om-comfyui/ # ComfyUI 生成层 MCP
├── captions/               # ★ 剪映级逐词高亮字幕
│   ├── capcut_ass.py
│   └── words.example.json
├── orchestration/
│   ├── comfy_guard.py      # ★ 显存/内存护栏（防崩盘）
│   └── config.env.example
├── bin/
│   ├── install-docker.sh   # 一条龙装 docker（需你自己跑，要 sudo）
│   ├── start-native.sh     # ★ 免 docker 启动
│   └── llm-smoke.py        # LLM 供应商体检
├── config/llm.env          # LLM 配置（已填好，600 权限，勿提交）
├── docs/FFMPEG.md          # ffmpeg 全局 vs 各自打包（重要）
├── docs/RESOURCE_GUARD.md  # ★ 崩溃判读 + 自动释放
├── docs/LLM.md             # ★ 模型选型 + 免费额度平台
├── .mcp.json / .cursor/mcp.json
└── AGENT_WORKFLOW.md       # agent 自然语言 SOP
```

## 快速开始

```bash
# 1) ComfyUI 单独起（独占 4080，在 /cfu/ComfyUI）

# 2) 起编排栈 —— 二选一
bash bin/start-native.sh            # ★ 免 docker，立刻能跑（推荐先走这条）
# 或（需先装 docker，见下）：
cd orchestration && cp config.env.example .env && docker compose up -d

# 3) 挂 MCP（让 agent 同时会生成 + 会剪辑）
bash orchestration/mcp-config/claude_commands.sh
```

### 装 docker（可选）

已经装好了（`docker.io 29.1.3` + Compose v2.40.3）。没装的用一键脚本：
`bash bin/install-docker.sh`（Agent 侧 `NoNewPrivs=1` 提不了权，必须你自己跑）。

```bash
bash bin/start-native.sh --docker       # 走 docker compose
```

> **⚠️ 一个真实的坑**：WorkBuddy 的 shell 会注入
> `DOCKER_HOST=unix:///run/user/1000/podman/podman.sock`，而那个 socket 不存在，
> docker 会一律报 `cannot connect to the docker API`。**先 `unset DOCKER_HOST` 再用。**
> 你自己的终端里通常没这变量。

**有必要用 docker 吗？** 不是必需，但**装了就用，收益是实打实的**：
n8n 靠容器能自动重启、升级只换镜像、不污染系统；pipeline-service 也一起托管。
真正的注意点是别把 ComfyUI 塞进容器——GPU 穿透要 nvidia-container-toolkit，
还没直接跑宿主机稳。

**已实测跑通**（n8n :5678 + pipeline-service :8000 双绿，
`/health` 返回 `comfyui_reachable: true`，`/guard/preflight` 返回 `ok: true`）。
过程中修掉三个真坑：

1. 镜像里没打 `comfy_guard.py` → `ModuleNotFoundError`。
   改成 build context 提到 `orchestration/`，两个文件一起 COPY。
2. **ComfyUI 只监听 `127.0.0.1:8188`**，容器走 `host.docker.internal`(172.17.0.1) 被拒。
   → pipeline-service 改用 `network_mode: host`，容器内 127.0.0.1 就是宿主机。
   （另一条路是给 ComfyUI 加 `--listen 0.0.0.0`，但那会把它暴露到局域网，不推荐）
3. n8n 工作流里写的 `http://pipeline-service:8000` 在 host 网络下解析不了
   → 改成 `http://host.docker.internal:8000`（n8n 仍在 bridge 网络，带 extra_hosts）。

### LLM 配置（多供应商自动切换）

```bash
set -a && . config/llm.env && set +a
python3 bin/llm_router.py status    # 看各家用没用、冷却到几点
python3 bin/llm_router.py chat "测试"
python3 bin/llm-smoke.py            # 单家体检（对话 + 工具调用）
```

**当前启用 6 家**（按 priority 从小到大尝试，省钱优先、付费兜底）：

| 优先级 | 供应商 | 模型 | 实测状态 |
|---|---|---|---|
| 5 | zhipu 智谱 | `glm-4.7-flash` | 免费，但约 1/3 概率限流（1305）→ 只冷却 1 分钟 |
| ~~15~~ | ~~volcengine 火山方舟~~ | `doubao-seed-2-1-lite-260915` | ⏸ **已停用**：账号未充值，开通模型即按量计费会产生欠费 |
| 20 | modelscope 魔搭 | `Qwen3.8-27B` | ✓ 稳定 |
| 25 | qianfan 百度千帆 | `ernie-4.5-turbo-128k` | ✓ 工具调用标准 |
| 30 | **tokenhub 腾讯** | `hy3` | ✓ 对话+工具调用均正常 |
| 32 | tokenhub-ds 腾讯 | `deepseek-v4.1-flash` | ✓ 厂商级冗余（同一把 key） |
| 35 | siliconflow | `DeepSeek-V3.2` | ✓ 质量最好、工具调用最稳（付费，兜底） |

> 腾讯那段踩了两个坑，已写进 `docs/FREE_API.md`：Base URL 已迁到
> `tokenhub.tencentmaas.com/v1`（旧端点对该 key 一律 401），且 `hunyuan-lite` 已下线改名 `hy3`。

**某家限额/报错会自动切下一家，不会停**。三类失败分开处理：限额 → 冷却后复用；
鉴权失败 → 直接停用（不浪费冷却）；瞬时拥塞 → 短冷却 1 分钟重试。全都冷却时强制
启用最早恢复的那家再试一轮，宁可慢也不停摆。

新平台注册清单见 `docs/FREE_API.md`，拿到 key 填 `config/llm.env` +
`config/providers.json` 打开 `enabled` 即可，代码不动。修好 key 后跑
`python3 bin/llm_router.py reset` 复活被停用的那家。

**⚠️ 硬边界**：词级时间戳（剪映级逐词高亮的前提）**没有任何聊天模型能给**，
必须走 ASR 强制对齐。LLM 只写文案，不许猜时间。

**TTS 已移除**：云端 CosyVoice2 效果不达标，配音走本地（ComfyUI TTS 节点 / IndexTTS / F5-TTS）。

## 剪映级字幕

```bash
cd captions
python capcut_ass.py -i words.example.json -o captions.ass -p capcut
ffmpeg -i in.mp4 -vf "subtitles=captions.ass" -c:v libx264 -crf 18 -c:a copy out.mp4
```
详见 `captions/README.md`（含 faster-whisper 取 word 时间戳的完整命令）。

## FFmpeg

**全局 ffmpeg 已装在 `~/.local/bin` + `/usr/local/bin`**（静态构建，带 libass）。
各项目自带 ffmpeg 的现象与统一方法见 `docs/FFMPEG.md`——建议通读，能省掉大量"明明命令对却报错"的排查时间。

## GPU 资源

整条链路**只有 ComfyUI 吃 GPU**。编码助手请用**云端 API**（Claude/Cursor/Qwen Code），
否则本地 27B LLM 会和 WAN-14B 抢 16GB 显存，无法同驻。详见 `AGENT_WORKFLOW.md`。

## 防崩盘（每次出片前必看）

**崩溃只有两种形态，判读错了会往反方向排查：**

| 症状 | 真实原因 |
|---|---|
| `torch.OutOfMemoryError` + **有 Traceback** | **显存**不足 |
| 日志戛然而止 + **无 Traceback** + 进程消失 | **RAM** 被内核 oom-killer SIGKILL |

「没有 Traceback」正是 RAM OOM 的**特征**，不是原因不明。查证要上抬到内核：
`journalctl -k --since "-30min" | grep -i "out of memory"`。

护栏已内置自动调用（`comfy_guard.py`，也可手工）：

```bash
python3 orchestration/comfy_guard.py status       # 看内存/swap/显存
python3 orchestration/comfy_guard.py preflight    # 出片前：不够自动释放，仍不够就拒绝开工
python3 orchestration/comfy_guard.py free         # 强制腾资源
python3 orchestration/comfy_guard.py watch        # 批量出片时开着，低于水位自动释放
```

`MCP 的 comfyui_generate` 和 `pipeline-service 的 /generate` 都已接好：
提交前预检、任务后按需释放（余量充足时不释放，避免模型反复加载）。
详见 `docs/RESOURCE_GUARD.md`。
