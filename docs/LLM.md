# LLM 选型与接入（SiliconFlow 主用 / ModelScope 备用）

## 一、结论：按用途分工，不是一个模型打天下

| 用途 | 选定模型 | 实测结果 |
|---|---|---|
| **编排大脑**（总指挥，跑 tool calling） | `deepseek-ai/DeepSeek-V3.2` | ✅ 强制工具调用返回标准 `tool_calls`，中文稳 |
| **副手 / 文案** | `Qwen/Qwen3.5-35B-A3B` | ✅ JSON 模式干净，但会吐 `reasoning_content`（慢一点、token 多） |
| **编码改脚本** | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | 未实测，定位就是代码 |
| **画面质检 / 抽帧审片** | `Qwen/Qwen3-VL-32B-Instruct` | 视觉 QA 用 |
| **旁白 TTS** | `FunAudioLLM/CosyVoice2-0.5B`（走 `/v1/audio/speech`） | ✅ 实测 200，返回 32KB mp3 |
| **备用（主用挂了自动切）** | ModelScope `Qwen/Qwen3.8-27B` | ✅ 实测可用 |

配法：`cp config/llm.env.example config/llm.env`，然后
`python3 bin/llm-smoke.py` 一键体检（对话 / 工具调用 / TTS 全测一遍，不打印密钥）。

## 二、为什么是 DeepSeek-V3.2 当大脑

编排大脑的要求很具体：**中文** + **稳定 tool calling** + **长上下文**（workflow JSON 很长）。

实测证据（同一份带 tools 的请求）：

```
V3.2   → {"content":"", "tool_calls":[{"name":"comfyui_generate",
          "arguments":"{\"workflow\": \"wan22-t2v\", \"params\": {\"prompt\": \"...\"}}"}]}
        ✓ 参数结构完全正确，finish_reason=tool_calls
Qwen3.5-35B-A3B → 也能出 tool_calls，但附带一大段 reasoning_content，
        每个决策多花几百 token 和几秒，编排场景不划算
```

V3.1-Terminus / V3 / R1 都能用，但：R1 是推理模型（慢，编排不需要深度推理）、
V3 是上一代。**V3.2 是列表里最新且性价比最高的**。

## 三、⚠️ 必须说清楚的：有件事 LLM 干不了

**词级时间戳（剪映级逐词高亮的前提）没有任何聊天模型能给。**

我实测问 V3.2 给"夜幕降临，霓虹亮起"切词打时间戳，它自己的回答是：

> "请注意，这是基于文字长度和常规语速的估算，并非通过实际音频分析得出的精确时间戳。"

LLM 是靠字数估的，**剪映那种"说到哪个字亮哪个字"必须来自 ASR 强制对齐**。
所以本栈的正确分工是：

```
文案（LLM 写）  ──┐
                  ├─→ ASR 强制对齐（ComfyUI 的 Qwen3-ASR 节点 / faster-whisper）
配音音频（TTS）──┘        ↓
                    词级时间戳 words.json
                          ↓
                    captions/capcut_ass.py → 剪映级 ASS → ffmpeg 烧录
```

**LLM 只负责写文案，绝不能让它猜时间。** 这条是硬边界，踩了字幕一定对不上嘴。

## 四、TTS：云端方案已弃用

SiliconFlow 的 `/v1/audio/speech` + `CosyVoice2-0.5B` 实测**能通**（返回 32KB mp3），
但**生成效果不达标**，已从本栈移除。

配音改走本地方案（ComfyUI 的 TTS 节点 / IndexTTS / F5-TTS），
显存由 `comfy_guard.py` 统一调度——提交前预检、任务后释放，避免和生成模型抢 16G。

> 教训：端点"通"不等于"能用"。音质这种主观指标，必须耳朵验过才算数。

## 五、更多有免费额度的 API（注册即可切换）

| 平台 | 端点 | 免费情况 | 备注 |
|---|---|---|---|
| **ModelScope**（已用） | `api-inference.modelscope.cn/v1` | 有每日免费额度 | OpenAI 兼容，Qwen 系最全 |
| **SiliconFlow**（已用） | `api.siliconflow.cn/v1` | 部分模型标 free（如 `Qwen/Qwen2.5-7B-Instruct`） | 模型最全，含 TTS/生图/视频 |
| **OpenRouter** | `openrouter.ai/api/v1` | 大量 `:free` 后缀模型 | 一个 key 走天下，方便对比各家 |
| **智谱 GLM** | `open.bigmodel.cn/api/paas/v4` | `GLM-4-Flash` 免费 | 中文不错 |
| **Groq** | `api.groq.com/openai/v1` | 免费但有速率限制 | 极快，适合轻量编排 |
| **Google AI Studio** | `generativelanguage.googleapis.com/v1beta` | Gemini 免费额度 | 长上下文强 |
| **阿里云百炼** | `dashscope.aliyuncs.com/compatible-mode/v1` | 新用户免费额度 | 通义全家桶 |
| **讯飞星火 / 腾讯混元** | 各自域名 | 有免费额度 | 中文场景 OK |

因为本栈统一走 **OpenAI 兼容格式**，切换只需改三个变量：
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`——代码一行不动。

## 六、给 Agent 工具链用的话

Qwen Code 用户注意：连接地址要写在 `security.auth.baseUrl`，
写在 `model.baseUrl` 不生效（后者只用于路由匹配）。
