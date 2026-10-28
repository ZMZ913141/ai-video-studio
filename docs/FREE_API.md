# 免费额度 API 平台注册清单

> 信息截至 2026-10-28。免费政策**变动频繁**，以各平台控制台当日显示为准。
> 注册完把 key 填进 `config/llm.env`，再到 `config/providers.json` 把对应家 `enabled` 改成 `true`
> ——路由会自动接管，**某家用完就切下一家，不会停**。

## 一、优先注册这 5 家（国内、稳、额度实在）

| 顺序 | 平台 | 注册地址 | 免费额度 | 备注 |
|---|---|---|---|---|
| 1 | **智谱 GLM** | <https://open.bigmodel.cn> | **GLM-4.7-Flash 永久免费不限量** | ★性价比最高，中文编码强，先注册这家 |
| 2 | **火山方舟（豆包）** | <https://console.volcengine.com/ark> | 约 200 万 token/天 | ⏸ **本栈已停用**（未充值，开通即按量计费）。国内日额度最大，按天重置。⚠ **key 拿到还不够，必须逐个开通模型**，见下方说明 |
| 3 | **阿里云百炼** | <https://bailian.console.aliyun.com> | 每模型约 100 万 token（90 天） | ⚠ 实名后超额会**自动转按量付费**，记得关掉 |
| 4 | **腾讯 TokenHub**（混元已并入） | <https://console.cloud.tencent.com/tokenhub/apikey> | 见控制台赠金/按量 | ⚠ **端点和模型名都换过**，见下方说明 |
| 5 | **百度千帆** | <https://console.bce.baidu.com/qianfan> | 部分模型免费额度 | 已实测：`ernie-4.5-turbo-128k` 可用；`ernie-lite`/`ernie-speed` 已下线无权限 |

**已注册并实测**：硅基流动 SiliconFlow（<https://cloud.siliconflow.cn>）、
魔搭 ModelScope（<https://modelscope.cn>，约 2000 次/天）、
智谱 GLM、百度千帆。

### ⚠ 腾讯：混元已并入 TokenHub，旧端点 + 旧模型名都会踩空

2026-10 实测，按这个排查顺序来（**前两条是真实踩过的坑**）：

| # | 坑 | 现象 | 正确做法 |
|---|---|---|---|
| 1 | **Base URL 已迁移** | 用 `api.hunyuan.cloud.tencent.com/v1` 打新 key → 一律 `invalid_api_key` | 改用 **`https://tokenhub.tencentmaas.com/v1`**（广州）<br>新加坡站点是 `tokenhub-intl.tencentmaas.com/v1`，**两站 key 不互通** |
| 2 | **模型名换过** | `hunyuan-lite` 已下线 | 现役：`hy3`、`hy4-preview`、`hy-mt2-lite`（翻译向）。<br>查全量：`GET /v1/models`，取 `status=online`（当前 119 个） |
| 3 | key 类型 | `AKID...` 是腾讯云 CAM SecretId，不是大模型 API Key | 用 `sk-` 开头的 API Key（TokenHub 控制台创建） |
| 4 | 模型未开通 | 返回 `402xxx` | 控制台里**先开通对应模型服务**才能调 |

**额外收益**：TokenHub 聚合了多家厂商，一把 key 可调 `deepseek-v4.1-flash`、`glm-5.3-flash`、
`kimi-k3`、`minimax-m3.1-flash` 等。本栈已开了两条通道（`tokenhub` 走 hy3、
`tokenhub-ds` 走 deepseek-v4.1-flash）做**厂商级冗余**。

**注意**：多数模型默认输出 `reasoning_content`，会把 `max_tokens` 吃光导致 `content` 为空。
本栈已统一传 `"thinking": {"type": "disabled"}` 关闭（`enable_thinking:false` 在该平台无效）。

### ⚠ 火山方舟：光有 API Key 调不通，还要「开通模型」

> **2026-10-28 更新：本栈已停用这条通道**（`providers.json` 里 `volcengine.enabled = false`）。
> 原因：账号未充值，而方舟「开通模型」即走**按量计费**，免费额度用尽/未充值会产生欠费。
> 哪天想启用：控制台开通模型 → 改回 `enabled: true` → `python3 bin/llm_router.py reset`。

实测：key 有效（`GET /api/v3/models` 正常返回 138 个模型），
但**账号未开通任何模型**，所有调用一律返回：

```json
{"error":{"code":"ModelNotOpen",
 "message":"Your account ... has not activated the model doubao-seed-2-1-lite-260915.
             Please activate the model service in the Ark Console."}}
```

注意它用的是 **HTTP 404**，很容易被误判成"模型名写错"。实际是**没开通**。

**开通步骤**：<https://console.volcengine.com/ark> → 左侧「模型推理」→ 开通管理 /
模型列表 → 找到目标模型点「开通」（通常是即时生效的免费额度开通）。

推荐开通 **`doubao-seed-2-1-lite-260915`**（豆包最便宜档，适合跑编排文案）；
预算够再加 `deepseek-v4-1-flash-261017` 或 `deepseek-v4-1-flash-economy-261017`。
开通后回到本目录跑一次 `python3 bin/llm_router.py reset` 立刻生效
（未开通期间它被冷却到次日 0 点，每天自动重试一次）。

> 路由已把 `ModelNotOpen` / `InvalidEndpointOrModel.NotFound` 归为 **config 类**：
> 冷却到次日 0 点并提示去控制台开通，不会 5 分钟一次空转重试。

智谱也有个坑：`glm-4.7-flash` 免费，但**约 1/3 概率返回「1305 访问量过大」**，
这是瞬时拥塞、不是额度耗尽，路由已按 1 分钟短冷却处理（见 `bin/llm_router.py` 的 `RATE_HINTS`）。
`glm-4.6`、`glm-5.3-flash` 等需付费，会报 `1113 余额不足`。

## 二、补充（看需求）

| 平台 | 地址 | 免费额度 |
|---|---|---|
| 讯飞星火 | <https://xinghuo.xfyun.cn> | Spark Lite 永久免费，但 QPS=2 很慢，只适合兜底 |
| Kimi | <https://platform.moonshot.cn> | 新用户赠送，长上下文 |
| MiniMax | <https://platform.minimaxi.com> | 代金券 |
| 百川 | <https://platform.baichuan-ai.com> | 赠金 |

## 三、海外（需要梯子，额度也很猛）

| 平台 | 地址 | 免费额度 |
|---|---|---|
| **OpenRouter** | <https://openrouter.ai> | 约 50 次/天（`:free` 模型），一个 key 走遍各家 |
| **Groq** | <https://console.groq.com> | 约 1000 次/天、30 RPM，速度最快 |
| **Cerebras** | <https://cloud.cerebras.ai> | 约 100 万 token/天 |
| **Google AI Studio** | <https://aistudio.google.com> | Gemini Flash 约 250 次/天 |
| GitHub Models | <https://github.com/marketplace/models> | 约 50 次/天 |
| NVIDIA NIM | <https://build.nvidia.com> | 注册送 credits |
| Cloudflare Workers AI | <https://dash.cloudflare.com> | 约 10K 请求/天 |
| Mistral / SambaNova | 各自控制台 | 有免费层 |

## 四、填好之后

```bash
# 1) 填 key
vim config/llm.env

# 2) 打开对应供应商（改 enabled: true，model 换成控制台里实际的 id）
vim config/providers.json

# 3) 看路由状态 / 实测
set -a && . config/llm.env && set +a
python3 bin/llm_router.py status
python3 bin/llm_router.py chat "测试一下"
```

## 五、自动切换是怎么判的

| 错误 | 分类 | 处理 |
|---|---|---|
| 429 / 配额不足 / 余额不足 / "额度已用尽" | **配额** | 冷却后**换下一家**；标了 `daily_reset` 的冷却到**次日 0 点** |
| 401 / 403 / key 无效 | **鉴权** | 直接停用（冷却没意义），提示改配置 |
| 5xx / 超时 / 网络错误 | **服务端** | 短冷却 5 分钟后重试 |

**不会停摆**：即使所有供应商都在冷却，路由也会强制提前启用"最早恢复"的那家再试一轮并告警。
冷却状态存在 `config/.llm_state.json`，`python3 bin/llm_router.py reset` 可手动清空。

## 六、免费的四种形态（别搞混）

1. **指定模型长期免费**（智谱 Flash、各家 Lite/Speed）——最省心，主力用这个
2. **按时间重置的额度**（火山、Groq、Cerebras）——真正的"永动机"
3. **新人一次性赠送**（百炼、Kimi、MiniMax）——有有效期，用完或过期就没；**留意会不会转扣费**
4. **永久免费但限速**（讯飞 Lite QPS=2）——只适合兜底

想长期挂在 n8n / agent 后面，选前两类。
