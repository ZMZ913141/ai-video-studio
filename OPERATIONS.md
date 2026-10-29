# 操作手册 · AI VIDEO STUDIO

> 给**实际操盘的人**看的：怎么把一条片子从「接客户」走到「交付」，以及出问题时去哪看。
> 深一点的原理、护栏细节、流水线每一步在干什么，见 [`RUNBOOK.md`](./RUNBOOK.md)（那是给「想知道为什么」的人看的）。
> 本手册只回答「**下一步点哪、敲什么**」。

---

## 0. 一句话架构

- **引擎**（这套代码）：在 `~/github/ai-video-studio/`，可进 git。
- **档案**（每个客户的素材/成片/文案）：在 `/cfu/ai视频档案库/<IP名>/`，**不设远程**，只做本地回滚。每个 IP 一份脚本副本（铁律）。
- **两台操作面**：
  - **看板**（本地网页，localhost）—— 看板、建档、写需求、改提示词、看自检、看日志、**安全动作**。
  - **终端**（命令行 `bin/vstudio.py`）—— 真正的**出片重活**（跑 GPU）只在终端或直接点看板的「出片」按钮，按钮背后就是同一条命令。

---

## 1. 起手式：把看板开起来

```bash
cd ~/github/ai-video-studio
bash bin/start-ui.sh            # 后台起，终端会打印真实地址（端口默认 8790，被占会自动顺延）
#   --fg       前台起，Ctrl+C 停（看得见日志）
#   --status   看现在地址
#   --stop     停掉上一次起的那个
#   --restart  停掉再起
#   --port 9000  指定端口
```

打开它打印的地址即可。看板右上角能看到服务徽章（ComfyUI / LLM 网关等）——灰了就是那个服务没起。

> 改了 `ui/` 下的前端文件**不用重启**看板（静态文件现读）；改了 `bin/` 下的 Python 才需要 `--restart`。

---

## 2. 接一个新客户（建档）

看板：顶栏「新建档案卡」。或终端：

```bash
python3 bin/vstudio.py card-new --name 王总财经 --type 客户 --tag M \
    --role "财经口播·竖屏·干货向" --aliases "老王,王总" --space 王总资产
python3 bin/vstudio.py guard 王总财经        # 缺真值会 exit 1 —— 不许硬跑
python3 bin/vstudio.py card-note 王总财经 "片子里不许出现同行品牌名" --kind redline
python3 bin/vstudio.py card-note 王总财经 "每周三交付两条" --kind need
python3 bin/vstudio.py card-note 王总财经 "字幕一律用品牌蓝" --kind habit
```

- `--aliases` **必填**：口头叫「老王」，目录是「王总财经」，没别名会重复建档。
- `--space` **必填**：不给会让建片脚本卡住等交互输入。
- 记成 `redline` 的，生成提示词时命中就**直接被拦**，不会先跑一遍再报错。

---

## 3. 开一套新片 + 写需求

看板：左栏选中 IP → 中栏 hero 区「开一套新片」→ 填套次号（自动顺排，别手改前缀）和标题。

终端等价：`python3 bin/vstudio.py job-new 王总财经 --set M01 --title "为什么我不用真人出镜"`

开完**先去中栏「需求台」把要求写清楚**——这是后面 AI 写文案和生成提示词的依据。写完点「存需求」。

---

## 4. AI 写文案（按这个角色的人设写爆款）

看板：需求台「写文案」按钮（会先自动存一次需求）。或终端：

```bash
python3 bin/vstudio.py llm ...          # 不，文案走 studio_ai
# 实际由看板「写文案」触发：按角色需求 + 主号爆款标准出稿，只调一次 LLM，不覆盖已有文件
```

- 出来的是**样稿**，先看味道；满意了再点「采用」落盘到 `05_文案/<套次>/`。
- 已有文案**不会被覆盖**：批量重跑时如果这套已经有稿子且没给新选题，就沿用。
- **落盘的文件名固定是 `<套次号>_字幕文本.txt`**（如 `M01_字幕文本.txt`）：
  目录名带标题（`05_文案/M01_老王的第一条…/`）是给人看的，**文件名只带套次号是给脚本看的**
  —— `batch.py` / `gen_tts.py` 都按这个格式找稿子。名字写歪了，批量出片会一片空白且不报错。
- **客户的稿子默认不出本机**。客户 IP 的 `card.json` 里 `policy.allow_cloud_llm=false`，
  点「写文案」会被拦（这是对的）。三条出路：改卡里那个开关、临时加
  `STUDIO_ALLOW_CLOUD=1`、或等本地模型接进路由。真客户别图省事改开关。

---

## 5. 提示词工作台（生成 / 优化 / 微调 + 红线）

看板：中栏「提示词」面板。

- **AI 生成**：按文案出每段画面提示词（中文意图 → 过护栏 → 落 `prompts.json`）。
- **AI 优化**：在现有基础上提质。
- **就地改**：每段「改」展开直接编辑；保存时**先过 ip.json 红线校验**，含红线词语不会落盘。

### 5.1 想让改的稿子真的出到画面上 → 译成 prompts.en.json

出片骨架是**英文**（H3 的受理格式）。中文不能直接塞进去——不在说话标记里的中文会被模型
当成「画面上该出现的字」，S04 就整片烧出过字幕，而且低分辨率试片还看不出来。所以中间要过一层：

```
prompts.json（中文分镜，你在这里改）  --to-en-->  prompts.en.json（英文）  -->  出片才认
```

```bash
python3 bin/vstudio.py prompts 王总财经 M01 --to-en            # 译成英文
python3 bin/vstudio.py prompts 王总财经 M01 --to-en --force    # 中文稿改过之后重译
```

面板顶部的状态条就三种意思，照着做即可：

| 状态条 | 含义 | 该做什么 |
|---|---|---|
| 「还没译成英文」 | 现在改稿**不影响画面**（不是 bug，是还没走到那一步） | 点「译成 prompts.en.json」 |
| 「中文稿改过，英文那版没跟上」 | 出片用的是**旧英文**（最难查的假片） | 点「按现在的中文稿重新译」 |
| 「英文分镜已就绪」 | 通了 | 直接出片 |

- 每次翻译会调一次 LLM（几十秒）。中文稿没变时不会重复调，直接提示「已是最新」。
- 没译这套片子照旧出（`骨架 + ip.json` 的 `prompt_vars`），行为跟以前完全一致。
- `--to-en` 出来的英文若混进汉字，会在**出片前**被 `gen_video.py` 拦停并报错——不会让你交一版带字幕的片出去。

终端等价：`python3 bin/vstudio.py prompts 王总财经 M01 --gen`（或 `--optimize` / `--set 3 --text "..."` 改第 3 段）。

---

## 6. 出片（一条龙）

看板：中栏「命令」区的「出片」按钮，或先点「看计划」（只算要跑哪些步、不烧 GPU）。

终端：

```bash
python3 bin/vstudio.py pipeline 王总财经 M01 --tier fast --dry   # 先看计划，不提交 GPU
python3 bin/vstudio.py pipeline 王总财经 M01 --tier fast          # 真跑（fast 快速档 ≈ 5 分钟）
python3 bin/vstudio.py pipeline 王总财经 M01 --tier std           # 标准档 ≈ 19–25 分钟
```

档位：`fast`(0.5MP) / `pilot` / `std`(1.0–1.26MP) / `high`(高清)。流水线固定顺序：
`gen_tts → make_srt_asr → gen_video → finish_v6 → mix_bgm → burn_ass`，之后 `srt_sync_audit` / `audio_check`。

跑起来后右下角有**运行面板**：日志实时滚、能「停止」（收尾当前段，不留半成品）、能「再跑一次」、能看「最近跑过」。

---

## 7. 三种重跑（别乱点「重跑」）

这是最容易踩的坑——「重跑」一个按钮有三条完全不同的语义，看板把它们拆开了：

| 想干什么 | 怎么点 / 敲什么 | 会发生什么 |
|---|---|---|
| **改了文案，只重做出受影响的部分** | 阶段弹层「补跑」/ `pipeline ... --from tts` | 输入没变就跳过，改了才从配音重做 |
| **整条不要了，全重做** | 阶段弹层「全部重做」/ `pipeline ... --force` | 不看输入，每一步重跑 |
| **画面不满意，换一版画面** | 阶段弹层「换种子」/ `pipeline ... --reseed`（或 `--seed N` 指定复现） | 从 video 及下游重出，种子变了 |

- 种子是一等参数：每次真跑记在 `08_过程/<套次>/runs.jsonl`，`--seed N` 可复现某一次画面。
- 断点续跑靠「源文件比产物新」：你改了 `05_文案`，重新跑会只重做出受影响那段。

---

## 8. 批量出片（一口气出多条）

> GPU 只有一个（4080S），所以批量是**排队串行**，不是并发。并行只会撞 OOM 被内核杀。

**看板**：顶栏「批量」按钮 → 选 IP → 每行写一条选题 →（可选）套次号逗号分隔、档位、勾「先看计划」/「出错即停」→「开始批量」。
非 dry 的批量会弹**二次确认**（要烧几十分钟 GPU），确认后后台跑，日志实时可见。

**终端**：

```bash
# 快捷：分号分隔选题
python3 bin/vstudio.py batch 王总财经 --topics "为什么不用真人出镜;一条片子多少钱" --tier fast

# 指定套次号（数量须与选题一致）
python3 bin/vstudio.py batch 王总财经 --topics "甲;乙" --sets M01,M02 --tier std

# 队列文件（JSON，适合很多条 / 要逐条带标题）
python3 bin/vstudio.py batch --queue 队列.json
#   队列格式见 bin/batch.py 顶部；每条可带 set / title / topic / script / tier

# 安全开关
python3 bin/vstudio.py batch 王总财经 --topics "甲;乙" --dry           # 只算计划，不烧 GPU、不建套次
python3 bin/vstudio.py batch 王总财经 --topics "甲;乙" --limit 1        # 先试跑前 1 条
python3 bin/vstudio.py batch 王总财经 --topics "甲;乙" --stop-on-error  # 某条失败就停（默认继续跑剩下的）
```

批量行为：
- 每条独立 job，失败不阻塞其他条（除非 `--stop-on-error`）。
- 条间做**资源预检**（`bin/comfy_guard.py preflight`）；预检不过就**停下整批**——继续跑只会一条接一条被内核杀。
- 结束给**汇总报告**，写在 `08_过程/batch-<时间戳>.json`（dry 的报告加 `_dry` 后缀，不和真跑混）。

队列文件格式：
```json
{
  "ip": "王总财经",
  "defaults": {"tier": "std"},
  "items": [
    {"set": "M01", "title": "为什么我不用真人出镜", "topic": "AI 分身和真人出镜的区别"},
    {"set": "M02", "script": "/path/to/已写好的文案.txt"},
    {"set": "M03", "topic": "一条片子多少钱", "tier": "fast"}
  ]
}
```
`set` 不写就按这个 IP 现有最大编号往后顺排。

---

## 9. 自检与常见故障

### 开工前：素材自检
看板「进度与自检」面板「跑自检」，或终端 `python3 bin/vstudio.py guard <IP>`。
缺真值（音色参考 / 静帧 / 形象底图）会 **exit 1**——别硬跑，先补齐再出片。

### 出片中途挂了，去哪看
- 运行面板日志（右下角），或「最近跑过」。
- 流水线详情：`08_过程/<套次>/pipeline-<时间戳>.json`。
- 批量：`08_过程/batch-<时间戳>.json`。

### 常见报错

| 现象 | 含义 / 处理 |
|---|---|
| `失败: ComfyUI 在 1800s 内没返回这条任务的状态……` | TTS/视频这步**GPU 推理没跑起来**：CUDA 不可用 / 对应 ComfyUI 节点缺模型 / ComfyUI 中途崩了。去看 **ComfyUI 控制台**，不是在终端里能修的。 |
| `资源预检 ✗ …批次中止` | 内存/显存不够。批量会主动停下，不要并行提交。等显存释放或换小档位再跑。 |
| `429 / 频率限制` | LLM 网关被限流，等重置（一般几十分钟）再试。AI 写文案/提示词会受影响，不影响已出的片。 |
| `提示词太短/太长` | 单段提示词要 8–1200 字，太少误触、太多模型抓不住。 |
| `不认识的 IP / 套次` | 名字写错，或这套还没建。先建档/开套次。 |
| `✗ prompts.en.json 第 N 段的 … 里混进了中文` | 英文转写里混进了汉字，**出片前已拦停**（中文会被烧成上屏字幕）。删掉 `prompts.en.json`，重跑 `--to-en --force`。 |
| `⚠⚠ prompts.en.json 跟当前中文稿不同源` | 改了中文没重译，此刻出片用的是**旧英文**（画面不反映你的改动）。照提示重译一次。 |
| 改了代码重启看板，正在跑的出片从界面消失 | 进程还在烧 GPU，只是界面丢了引用。用 `bin/start-ui.sh --restart` 后看板会把活着的任务接回来，别重复点出片。 |
| `✗ 可用内存 XX G < 要求 40G` | **别直接跳过护栏**（`SKIP_MEMCHECK=1`）——那正是 2026-10-27 S04 丢掉 20 分钟 GPU 时间的场景。先查是谁吃了内存，见下面「内存被占满怎么查」。 |

### 内存被占满怎么查（护栏拦下来时的正确反应）

护栏拦的不是「机器内存小」，是「**有进程占着不放**」。2026-10-29 实测查出的完整链条：

```bash
curl -s http://127.0.0.1:8188/queue        # ← 第一步：看 ComfyUI 队列里压着几个任务
curl -s -X POST http://127.0.0.1:8188/interrupt   # 中断正在跑的
curl -s -X POST http://127.0.0.1:8188/queue -H 'Content-Type: application/json' -d '{"clear":true}'
grep MemAvailable /proc/meminfo        # 中断后内存通常立刻回来
```

**实测**：队列里卡着一个早就提交、一直没跑完的 H3 任务（UNET + Qwen3VL-32B + VAE 全套占 46G）。
`comfy_free.py --free` 释放不了（它只清 ComfyUI 的模型缓存），因为**任务本身还在跑**。
中断后 MemAvailable 从 11G 回到 47.5G，护栏立刻放行。

判据：
- `MemAvailable` 低 + ComfyUI 队列非空 → **队列里的任务才是元凶**，去 interrupt
- `MemAvailable` 低 + 队列空 → 有别的进程占着（本机沙箱看不到主机进程，需要你在自己终端查 `ps aux --sort=-%mem | head`）
- `MemAvailable` 低 + `buff/cache` 高 → 那部分可回收，不算真占

> ⚠ 别看到护栏拦就加 `SKIP_MEMCHECK=1`。护栏的意义是「宁可现在停 3 秒，
> 不要 20 分钟后被内核 SIGKILL、GPU 时间全废」。

### 清理（只用回收站，不用 rm）
- 不要的素材/成片 → 丢 `/cfu/废品站/<日期>_<描述>/`，不要真删。
- 废品站命名：`2026-10-29_香云纱马甲_TTSv2_气口偏多` 这种「日期_内容_原因」。

---

## 10. 前端按钮自测（改完前端必做）

**静态扫 HTML 没用**：看板里绝大多数按钮是 `app.js` 动态生成的，
扫源码只能看到 16 个冰山一角。真点一遍才知道点了会不会有反应。

### 10.1 三轮递进（前一轮看不见后一轮的问题）

| 轮次 | 判什么 | 工具 | 2026-10-29 实测 |
|---|---|---|---|
| ① 静��交叉 | 按钮 id 在 app.js 里有没有被引用 | 见下方「静态自查」 | 77 个 id **零漏网** |
| ② 真点（不看后端） | 点了有没有反应 / 有没有抛异常 | `.cache/uiprobe/deep.js` | 1365 次点击，零 console 错误 |
| ③ 真点（**看后端回什么**） | 后端有没有回 error / 5xx | `.uitest/click_api_effect.js` | 1365 次点击，373 个 api-error **全是设计内护栏** |

★ 第③轮最有价值：前两轮只判「前端有没有反应」，
**「按钮接上了、但后端逻辑是坏的」这类问题前两轮完全看不见**。
2026-10-29 就是靠它抓到「清空输入框点保存 → 界面弹 Python 的
`could not convert string to float: ''`」。

### 10.2 工具

| 脚本 | 干什么 |
|---|---|
| `.cache/uiprobe/domprobe.js` | 首屏全部按钮真点一遍（干净状态，最快） |
| `.cache/uiprobe/deep.js` | 切 tab × 每个 IP × 每个套次，逐个上下文重扫 |
| `.cache/uiprobe/loop.js` | 完整闭环：写文案 → 等 AI → 「写进这套片子」→ 验证落盘 |
| `.cache/uiprobe/create.js` / `newset.js` | 走前端表单建档 / 建套次 |
| `.uitest/click_api_effect.js` | **第③轮**：看后端回了什么（.uitest 已 gitignore） |
| `.uitest/test_empty_value.js` | 单点回归：空值拦截 |

跑法（另开一个终端起看板，用 `--force` 绕开「已有一个实例」守卫）：

```bash
python3 bin/ui_server.py --port 8798 --force &
NODE_PATH=/home/qmr/.workbuddy/binaries/node/workspace/node_modules \
  node .cache/uiprobe/deep.js http://127.0.0.1:8798
# 第③轮
NODE_PATH=/home/qmr/.workbuddy/binaries/node/workspace/node_modules \
  node .uitest/click_api_effect.js http://127.0.0.1:8798
```

判定分四档：`api-ok`（点了调了接口）/ `reacted`（界面变了）/ `silent`（毫无反应，**要查**）/
`js-error`（抛异常，**要查**）。

### 10.3 静态自查（不点也能查漏）

```bash
# ① 有没有按钮在 app.js 里完全没被引用（词边界匹配，别用朴素 in）
#    ⚠ 用 `'btnX' in app` 会漏掉 `$('#btnX')` —— 必须用正则词边界
# ② 模板里用 data-* 的按钮，有没有消费代码
#    ⚠ 消费可能是 dataset.setKey（camelCase）或 querySelector('[data-set-key]')
```

2026-10-29 实测：静态**零漏网**。但静态只能证明「接了」，证明不了「接对了」——
还是得真点。

### 10.4 坑（都是实测踩的）

- 本沙箱里 **Chromium 一访问看板就崩**（`ERR_INSUFFICIENT_RESOURCES`），
  headless_shell 也被 SIGKILL → **用 jsdom，别用真浏览器**。
- jsdom 里 `fetch` 要**真转发到后端**，不能用 mock：mock 返回空对象，
  前端拿到 `undefined` 直接崩，测不出真问题。
- 点按钮前**重新从文档里找元素**（按 id）：前端点完常重建 DOM，
  拿旧引用 click 会打在孤儿节点上 → 假的「毫无反应」。
- **每个上下文之间要重新加载页面**，否则前一个上下文点坏了状态，后面全是假阳性。
- 危险按钮（停止/重置/删除）**不要点**。
- ⚠ **沙箱里 pkill 杀不掉主机上的 ui_server**（跨 PID 命名空间），
  端口会被旧实例占着 → 测新代码要**另起端口**（`--port 8805`），
  拿旧端口测到的还是旧代码（2026-10-29 就被这个骗过一次）。

---

## 11. 文件去哪了

```
/cfu/ai视频档案库/<IP名>/
  01_人设与规范/   02_素材真值/   03_生产脚本/   04_验收工具/
  05_文案/<套次>/<套次号>_字幕文本.txt     # 文件名只带套次号（脚本按这个找）
  05_文案/<套次>/prompts.json       # 中文分镜（人在画面板上改的就是它）
  05_文案/<套次>/prompts.en.json    # 它的英文转写（--to-en 产出，出片只认这份）
  06_音频/<套次>/<套次>_音频_定板_Ns.mp3
  07_成片/<套次>/
  08_过程/<套次>/pipeline-*.json        # 单次流水线明细
  08_过程/batch-*.json                  # 批量汇总报告
  card.json      # 业务/习惯/红线（人话）
  ip.json        # 技术真值（音色/静帧/模型/档位）—— 出片只读这里
```

- `card.json` 与 `ip.json` **不重复**：卡管业务/习惯/红线，ip.json 管技术真值。改参数只改一处。
- 引擎改动（bin/ config/）**不动档案**；改某个 IP 的脚本副本**不动引擎**。
- **字体不在档案里**：装在系统 `~/.local/share/fonts/`，所有 IP 共用系统的，
  不存副本目录（2026-10-29 实测 libass 不给 fontsdir 走 fontconfig，
  输出与给字体目录逐字节一致，内存还少一半）。

---

## 12. 与 RUNBOOK 的关系

- **本手册（OPERATIONS.md）**：操作清单，照着点/敲。
- **RUNBOOK.md**：设计原理、护栏为什么这样设、每段脚本在干什么、怎么加新 IP/新模型、排障的底层逻辑。
  真遇到手册盖不住的怪事，去 RUNBOOK 翻对应章节。
