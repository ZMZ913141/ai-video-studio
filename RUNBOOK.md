# 运行手册

> **原则：界面是可选项，接口才是本体。**
> 四个入口，同一批底层函数，能力完全相同：
>
> | 入口 | 谁用 | 怎么用 |
> |---|---|---|
> | **CLI** | 人、我（WorkBuddy）、任何能跑 Bash 的 agent | `python3 bin/vstudio.py <命令>` |
> | **MCP** `ip-studio` | 任何支持 MCP 的 AI（Claude Code / Cursor / Qwen Code） | 挂上即用，11 个工具 |
> | **HTTP** `:8000` | n8n / 外部系统 | pipeline-service REST |
> | **本地网页** `:8790` | 人（看板 + 安全动作） | `bash bin/start-ui.sh` → 见「一之二」 |
>
> 三个护栏：**① 界面不发明逻辑**（按钮背后就是 CLI）；
> **② 同一参数只存一处**（界面只读真值文件那一份）；
> **③ 出片类重活界面能跑，但必须看得见**：点「跑」→ 二次确认 → 后台执行 →
> 右下角日志实时滚 / 随时能停 / 跑完自动刷新。只想拿命令就点「复制」。
> 界面依然不发明命令：前端只报「要做什么」，argv 由后端白名单拼，不经 shell。

---

## 〇、目录：引擎与档案彻底分家

```
ai-video-studio/
├── bin/            vstudio.py（CLI）、mcp_server.py、llm_router.py   ← 引擎
├── mcp-servers/    om-comfyui（生成）、ip-studio（控制面）
├── orchestration/  n8n + pipeline-service + comfy_guard
├── captions/       剪映级 ASS 字幕
├── config/         providers.json（LLM 路由）、llm.env（key）
├── docs/
├── registry.json   ★ 总册：谁有档案卡、卡在哪
└── templates/      档案卡模板
```

**档案不在项目里**，在独立存储区（生产盘 `/cfu`，1TB 可用）：

```
/cfu/ai视频档案库/
└── 陈洁斌主号/          ← 一个 IP（角色/客户）一个目录
    ├── card.json      ← 业务层：需求/习惯/红线/数据策略
    ├── ip.json        ← 技术层：音色/静帧/采样参数（真值）
    ├── 01_人设与规范/  02_素材真值/  03_生产脚本/  04_验收工具/
    ├── 05_文案/  06_音频/  07_成片/  08_过程/
    └── 资产/          交付件（本 IP 私有，别家看不见）
    （新客户照抄一份，用 `card-new` 建，别手建）
```

> 换盘/换位置：`export STUDIO_ARCHIVE=/新路径`，CLI 即跟随。

**改动分两边，互不干扰**：

| 改什么 | 动哪里 | 影响 |
|---|---|---|
| 引擎升级（新模型、新字幕样式、路由策略） | `bin/` `orchestration/` `captions/` `config/` | 所有 IP 受益，**档案卡一个字都不用改** |
| 个人档案（换音色、改习惯、加红线） | `/cfu/ai视频档案库/<IP>/` | 只影响这个 IP，**引擎不动** |

**card.json 与 ip.json 不重复**：卡管业务与习惯，ip.json 管技术真值。
两份文件写同一个参数 = 迟早不一致 = 出废片（这是踩过的教训，见 `docs/`）。

---

## 一、日常三件事

```bash
cd /home/qmr/github/ai-video-studio
V="python3 bin/vstudio.py"

$V status        # 总览：几张卡、几套、服务在不在
$V cards         # 所有档案卡
$V jobs 洁斌      # 套次进度
$V guard 洁斌     # 开工前自检（素材不齐非零退出）
```

---

## 一之二、本地网页工作台（2026-10-29 上线）

**它是驾驶舱，不是第二条生产链路。**

```bash
cd /home/qmr/github/ai-video-studio
bash bin/start-ui.sh            # 后台起，打印真实地址（推荐）
bash bin/start-ui.sh --fg       # 前台跑，Ctrl+C 停，日志直接刷屏
bash bin/start-ui.sh --open     # 起完顺便开浏览器
bash bin/start-ui.sh --status   # 有没有在跑、地址是多少
bash bin/start-ui.sh --stop     # 停掉上一次起的那个
bash bin/start-ui.sh --restart  # 停掉再起
bash bin/start-ui.sh --port 9000
```

脚本只做三件手工容易做错的事：**先扫掉没留小纸条的旧实例**（否则你打开的是会乱码的初版）、
**等小纸条出现再报地址**（端口会顺延，`_8790_` 不能当默认答案）、
**起之前发现已经在跑就不起第二个**。

不想用脚本也行，底层就是下面这四个开关：

```bash
python3 bin/ui_server.py --port 8790
python3 bin/ui_server.py --status
python3 bin/ui_server.py --stop
```

端口撞车会自动往后顺延，**所以别靠命令行猜地址** —— 起完看这三条中的任意一条：
启动时的那行 `✓ UI 服务已启动 http://…`、或者 `--status`、
或者小纸条 `/tmp/vstudio-ui.json`（记着 pid、真实端口、启动时间，`--stop` 就是读它）。

手工清旧实例（脚本识别到会自己试一次，同一个终端里多半一次就过）：

```bash
pkill -f "bin/ui_server.py"; sleep 1
python3 bin/ui_server.py --status          # 应答「没在跑」
```

> `--stop` 动手前会先读 `/proc/<pid>/cmdline` 验明正身：PID 会被系统复用，
> 号对着但人不对时它拒绝执行，让你自己去 `ps -p <pid> -o pid,cmd` 看清楚。

### 它能干什么

| 区 | 内容 | 落到哪个命令/文件 |
|---|---|---|
| 左栏 | IP / 套次树 + 搜索 + 标签筛选；生产脚本清单；脚本偏离 | `vstudio ips/jobs/scripts/scripts-diff` |
| 中栏 | 阶段时间线、缺件、素材自检、素材与成片（缩略图 + 播放 + 文本预览）、提示词、命令串 | `job-show` / `guard` / `prompts.json` |
| 右栏 | 检查（档案卡/红线/习惯/技术真值）、设置（两层两类）、变更记录 | `card.json` / `ip.json` / `config/pipeline.json` |

### 界面边界：能跑，但必须看得见（2026-10-29 改口）

早期口径是「出片只给命令，界面不给跑」。这是**错的**：人要的不是一行能复制的命令，
而是「现在能不能开始、跑到哪了、不对能不能停」。改成：

| 动作 | 界面怎么处理 |
|---|---|
| `index --write` / `job-set` / `job-new` / `card-new` / `card-note` / `scripts-sync` | 直接执行（`scripts-sync --force` 要确认） |
| `pipeline` 出片、`prompts` 生成/优化、`ai-topics`、`ai-card-draft`、`guard`、`algo` | **后台跑**：二次确认 → 右下角日志实时滚 → 随时能停 → 跑完自动刷新 |
| 只想拿命令 | 每条后面都有「复制」；`--dry` 那条叫「看计划」，不花 GPU、不用确认 |

三条护栏没变，只是第三条改成新的：

1. 界面不发明逻辑 —— 按钮背后就是 CLI，argv 由后端 `build_exec()` 拼，**不经 shell**
2. 同一参数只存一处 —— 界面只读 `ip.json` / `card.json` / `config/pipeline.json`
3. **出片类重活界面能跑，但必须看得见** —— 日志实时、能停、停要停整个进程组
   （`start_new_session` + `killpg`，父进程和孙进程一起收，不留占 GPU 的孤儿），
   跑完落 `summary.json`；`--dry` 降为 tier1，免得「看一眼计划」也要点两次确认

**为什么必须能停**：出片一次十几分钟。一个只能开始不能停的按钮，等于把「手滑」升级成
「白等二十分钟 + 烧掉一张显卡的时间」。关面板 ≠ 停任务（关窗就杀进程是灾难）。

### 设置的两层两类

```
第一层 常用          全局：默认档位/CRF/每行字数/重叠帧/全局片段开关
                    个人：分辨率 MP/字号/语气指令
第二层 高级（折叠）  全局：通用片段正文、解释器与 ffmpeg 路径、红线阈值
                    个人：CFG/步数/LoRA 强度/Sage/三个字体名
```
- 第二层保存前**必须二次确认**，改完自动追加 changelog：
  全局 → `config/.ui_changelog.jsonl`；个人 → `<IP>/08_过程/_UI变更.jsonl`
- 每次写真值文件都会先 `cp` 一份 `.bak.<时间戳>`（已 gitignore）
- ip.json 里大多数参数**故意不可写**（采样器/二采模型/画幅/UnET）——这类动一处要重介整条链路，只留给脚本调用

### 每件事在哪做（2026-10-29 按用户实际卡点补的）

界面不是把 CLI 全搬上来，而是让"下一步该干什么"有地方点。对照表：

| 你想干的事 | 在界面的哪里 | 落到什么 |
|---|---|---|
| 听一下配音对不对 | 「素材与成片」点音频文件 | 弹层 `<audio>`，可拖进度（走 Range） |
| 开一套新片 | 中栏顶部「＋ 新套次」 | `vstudio job-new <IP> <编号> --title <标题>`（★ 标题必须走 `--title`） |
| 接一个新客户 | 左栏底部「＋ 新建档案卡」 | `vstudio card-new`（名字/别名/空间名必填） |
| 开工前把要求说清楚 | 「需求台」写、点「存需求」 | 落 `<IP>/05_文案/<套次>/brief.md` |
| 让 AI 按需求出提示词 | 需求台「生成提示词」 | 后台跑，日志里能看见被哪条红线拦下 |
| 改了字体/字号/某一段 | 「命令」面板最下面「断点重做」 | `pipeline … --from <步骤>` |
| 参数被改乱了想回退 | 设置里每项右边的「默认」 | 取 `git HEAD` 的值，改前自动备份 |
| 换字幕字体并看效果 | 设置 → 个人 → 「字幕外观」 | 选字体 + 拉字号，实时预览 |
| 看某个服务是死是活 | 顶栏四个圆点，点一下 | 弹层给地址、作用、怎么起 |
| **换 LLM 车道** | 顶栏点「LLM 网关」圆点 | 车道列表：设首选 / 单条实测（不写冷却） |
| **没想好拍什么** | 新套次弹层「AI 想几个选题」，或标题留空直接建 | 读 `01_人设与规范/` 出 5 条，过红线后给候选 |
| **接新客户说得清就行** | 「＋ 新建档案卡」→ 用大白话介绍 → AI 出草案 | 出字段 + 两篇人设 md，改到满意再点创建 |
| **看某一步的产物 / 从这步重跑** | 「进度与自检」点任意阶段节点 | 产物可点开预览 + 两条重跑命令（只重做这段 / 从这做到最后） |
| **交付前确认能不能交** | 点「交付」节点 | 自动查各阶段状态 + **实测成片分辨率** 对不对得上目标 MP |
| **让别的 AI 调用本项目** | 顶栏「MCP 接口」圆点 | 真握手一次，列工具 + 给注册命令 |
| 找设置 | 顶栏齿轮，或右栏「设置」tab | 同一个地方 |

几条口径：

- **「默认」= 上一次 git 提交的版本**，不是某个出厂常量。界面上试过但没提交的改动会被它撤掉。
- **字体预览只是浏览器渲染**，能不能真烧进片子看下拉里有没有 `✓`：有 `✓` 的才是
  `<档案库>/_共用/字体_字幕标题/` 里的文件。字体是**全库共用**的，不跟 IP 走
  —— 想加字体就往共用目录放 ttf，**别一次塞几百个**
  （libass 给大目录会吃爆内存，给空目录则静默回退 Noto、字形全错还不报错）。
  某个 IP 临时想换一套字体：跑脚本时给 `SUB_FONTS_DIR=/某目录`，别去改共用目录。
- **改完字幕外观不会自动重出片子**：要重跑 `ass` + `burn` 两步（断点重做里点「从 ass 起」）。
- **提示词生成现在在界面里跑**：日志在右下角逐行出，走哪家车道、被哪条红线拦下都看得见。
  第一家长时间无响应时路由会自动冷却它并换下一家 —— 那几行也会打在日志里，不会白等。
- **没文案就别点「生成提示词」**：提示词是照着文案逐段切的。`05_文案/<套次>/` 里没有 txt、
  `07_成片/` 里也没有 srt 时，点下去会当场说清缺什么（**不会**先让你点一次确认、
  再等十几秒收到一句「切不出段」—— 那种体验跟「点了没反应」是一回事）。

### 两条主路径（用户问的就是这两个，照这个走）

**① 怎么成功建一张新卡、开一套新片**

```
左栏底「＋ 新建档案卡」
  → 用大白话说清：他是谁 / 讲什么 / 发哪里多久一条 / 什么绝对不能碰
  → 「AI 出草案」（后台跑，右下角日志能看到它在试哪家车道）
  → 草案可逐字改；名字=目录名，落盘后改名很脏，所以判断放在不可逆之前
  → 「确认创建档案」→ 自动建整套目录 + card.json + 两篇人设 md + 登记总册
  → 建完**自动停在新卡上**，并提示下一步
右上「＋ 新套次」
  → 编号按这个 IP 的 tag 自动推（老王的 M 不会给你 S03）
  → 标题可留空（留空就按人设自动出一条），或点「AI 想几个选题」
  → 「建」→ 写 job.json + 进索引 + 自动选中这一套
再往后才是：需求台写 brief → 生成提示词 → 「开跑（先看计划）」
```

**② 怎么从某个阶段重跑**

```
「进度与自检」点任意阶段节点
  → 弹层里两条路，都带「看计划（dry）/ 跑 / 复制」三个按钮：
     只重做这一段      e.g. --from ass  --to burn
     从这一段做到最后  e.g. --from video
  → 「看计划」不花 GPU、不要确认；「跑」才二次确认
  → 跑起来后右下角日志实时滚，随时能停；关掉面板不会杀进程
```

**「点了没反应」的三次真实成因**（都已经修掉，别再从别处找）

1. `job-new` 的标题当位置参数传 → CLI 报 `unrecognized arguments` 退出，界面却当成功。
   现在必须 `--title`，且**要回执**：`rc != 0` 就报错，不再把失败当成功。
2. 新建的套次还没进索引时，`j.done.length` 抛异常 → 整页渲染静默中断，
   按钮看着能点，其实什么也没发生。现在一律 `(j && j.done || []).length`。
3. 动作是 tier2（要花钱/覆盖产物）→ 后端回 428，前端弹二次确认。
   这类"没反应"其实是**在等你点确认**，弹层在屏幕中间；现在文案会直说花的是什么
   （出片=吃 GPU；生成提示词=花一次调用费、不动显卡）。

### 冒烟（改完前端跑一次）

```bash
NODE_PATH=/home/qmr/.workbuddy/binaries/node/workspace/node_modules \
  node ui/smoke_ui.js
# 打印各区块渲染计数 + 抓运行时异常；有新 error 就是 regression
```

**真点按钮的回归**（jsdom 加载真 `app.js` + 真后端，不出图也能验点击）：

```bash
export NODE_PATH=/home/qmr/.workbuddy/binaries/node/workspace/node_modules
N=/home/qmr/.workbuddy/binaries/node/versions/22.22.2/bin/node
$N .uitest/click_video_run.js        # 阶段面板「跑」→ 过确认 → 真启动出片
$N .uitest/click_newcard.js          # 新建档案卡（手填路径 + 必填校验）
$N .uitest/click_newcard_newset.js   # 新建套次，建完核对左树里有没有
$N .uitest/click_prompts_guard.js    # 没文案被拦 / 有文案进确认
$N .uitest/click_resume_stop.js      # 刷新页面接回在跑的任务 → 面板上停掉
$N .uitest/click_done_check.js       # 成片跑完后各阶段状态 + 交付前自检
/home/qmr/.workbuddy/binaries/python/versions/3.13.12/bin/python3 .uitest/test_stop.py   # killpg 收干净（含孙进程）
/home/qmr/.workbuddy/binaries/python/versions/3.13.12/bin/python3 .uitest/test_adopt.py  # 重启接管三种分支
/home/qmr/.workbuddy/binaries/python/versions/3.13.12/bin/python3 .uitest/test_ip_policy.py  # 客户内容不许上云
```

`.uitest/` 不进版本库（本机脚本）。**沙箱里 chromium 起不来**（渲染进程必崩，
`dump-dom` 返回 0 字节），所以才用 jsdom；真实浏览器里请自己目视一遍留白和图标大小。

### 四件「会动脑子」的活，提示词都在 `bin/studio_ai.py`

界面只是入口，逻辑在 `bin/studio_ai.py`（也能直接跑，不必开网页）：

```bash
python3 bin/studio_ai.py persona 洁斌          # 看多少字进了 context
python3 bin/studio_ai.py topics 洁斌 -n 5      # 按人设出选题
python3 bin/studio_ai.py card-draft "客户是开潮汕牛肉火锅的…"
python3 bin/studio_ai.py script 洁斌 --set S06 --topic "一条片子值多少钱"
python3 bin/studio_ai.py script 洁斌 --set S06 --write   # 同时写进 05_文案（原稿先备份）
```

**为什么 AI 选题必须先读 `01_人设与规范/`**：没有人设就只能编，编出来的身份
和真人对不上，一开口就废。所以材料为空时它**直接拒绝**（不是给一堆烂选题），
并且把「去补哪份文件」写清楚。

**这几条一律 `sensitive=True`（只走付费池）**：人设圣经和客户档案是商业信息，
送进「提示词可能被上游记录」的免费池，是拿客户机密换几厘钱。这一行不许改。

**建档是两段式**：先出草案给人改，再落盘。因为档案卡一落盘就进总册、
名字等于目录名，事后改名很脏 —— 判断要发生在不可逆之前。

### 爆款文案：判据是一份文件，不是模型的网感

`templates/script_standard.md` 是**全库共用**的出稿判据，从主号 S01–S05
（都已交付、跑过量的）反向提纯出来的。五段骨架：

> 钩子 → 共情 → 反转或算账 → 降门槛 → **落点**

改这份文件等于改所有 IP 的出稿标准。三条硬规矩：

1. **落点按客户业务定，存在 `card.json` 的 `landing` 字段，一家一条，之后每条原样用。**
   主号那句「私信两个字，分身」对火锅店是废话 —— 落点不通用。
   没有 `landing` 时，第一次出稿会让 AI 拟一条，**必须人确认后存进卡里**，
   不能每次临时编（每次不一样 = 没有落点）。
2. **落点不靠模型自觉**。实测五次有四次模型只在 `landing` 字段里给一句、正文里不写，
   所以 `write_script()` 会自己把它拼到正文最后一行。漏了落点，整条片子等于白讲。
3. **字数不达标自动重写一次**。模型很容易只写「钩子 + 落点」两段（93 字 / 18 秒），
   五段塌成两段。卡不住就带着具体差距（"上一版只有 93 字，要 130–170"）再问一遍；
   还不行就如实报出来，不假装合格。

自检项（`checks`）：字数区间、落点是否在结尾、禁用词、红线、钩子能否独立成立。
**不过就不许落盘** —— 稿子照样显示给人看，但要改完再配音。

### 建完卡别就这么散了

`新建档案卡 → AI 出草案 → 确认创建` 之后会弹「下一步做哪个」：

- **① 按这个角色写一条爆款文案**（样稿，还没有套次也能写）
  人刚填完一堆需求，最想知道的是**这个角色开口什么味道**，不是文件夹建在哪。
- **② 开第一套片子**

上云许可在建卡时当场选，写进 `card.json` 的 `policy.allow_cloud_llm`：
本人 IP 默认允许，客户默认不许（走本机模型）。**这个字段以前根本没人写**，
于是新卡一律按「禁止上云」处理，而本机模型又常常没起 ——
新卡看起来"AI 功能全是坏的"，其实是被自己的安全默认值挡住了。

### LLM 车道：能选，但选了不排他

```bash
python3 bin/llm_router.py status                    # ★ 标出首选
python3 bin/llm_router.py prefer --provider qianfan # 设首选（传空 = 清掉）
python3 bin/llm_router.py probe --provider zengate  # 只测一条，且不写冷却
```

- 首选存在 `config/.llm_prefs.json`（本机偏好，不进版本库）。
- **首选只是「优先」不是「只用」**：这条 429/超时了照样自动换下一家。
  刻意不做「只走这一条」—— 那等于把整条流水线焊死在一家身上，正是
  「zen-gate 不能用就卡住」的成因。
- `probe` **不写冷却**：诊断动作污染真实路由状态，会让你以为某条坏了。

### 已知实现讲究

- **不走 CDN、不引框架**：`ui_server.py` 是 `http.server` 零第三方依赖，前端无构建。断网照样能开。
- **中文路径**：`http.server` 把 request line 按 iso-8859-1 解的，直接拿 `self.path` 会把「洁斌」读成乱码 —— 服务端统一重解码。
- **端口纸条放 `.cache/ui.port.json`，不放 `/tmp`**：`/tmp` 会被系统清理，容器里还常常是
  每次调用独立的（我就在这种环境里被坑过：`--status` 永远说「没在跑」）。
  纸条只在**自己还是纸条主人**时才删 —— 以前无条件删，会出现「旧实例退出 → 把新实例的纸条删了」。
- **同一台机器只跑一个实例**：启动前扫一遍端口问 `/api/ping`（回 `{"app":"ai-video-studio",...}`）。
  已经在跑就报地址并退出，确实要再起一个加 `--force`。
  不这么做，端口顺延会让你对着**旧进程**验证新代码，然后怀疑自己改错了。
- **改完代码重启看板，不会丢正在跑的任务**：起跑时落一份 `.cache/runs/<tid>.state.json`，
  启动时 `adopt_runs()` 把还活着的进程接回来（能继续看日志、继续停）。
  拿不到退出码时**如实写「按日志末尾推断」**，不瞎猜成成功。
  跑完的任务由 `load_history()` 从 `.summary.json` 回填最近 20 条 —— **只写不读的落盘等于没落**，
  以前每次重启「最近跑过什么」都是空白，磁盘上却存着一份份 summary。
- **`--stop` 不按 pid 杀**：带一张一次性暗号发 `/api/shutdown`，让服务自己收尾退出。
  原因：容器/沙箱里每次命令行调用可能是**独立的 PID 命名空间**，纸条上那个号在自己这边
  可能指向别的进程 —— 实测按 pid `kill` 把自己的 shell 杀了（`rc=143`）。
- **判服务活没活，必须看响应内容**：这套环境在 `127.0.0.1` 前面有一层反向代理，
  后端没了它会回 **502 `upstream connect failed`**。所以 `curl -s ... && echo 活着`
  会把"已经死了"判成"活着"。一律走 `ping_port()`（要求响应里有 `"app":"ai-video-studio"`）。
  同理， zen-gate 的 `/v1/models` 没带 key 回 401 —— 那是**活着要钥匙**，不是 down。
- **刷新页面也能接回任务**：`boot()` 末尾 `resumeRunning()` 会找 `state==running` 的那条，
  出片动辄十几分钟，中途刷一下页面是常态 —— 以前刷新后既看不到进度也停不掉。
- **阶段状态以产物为准**（`normalize_stages`）：`job.json` 的 `status` 只说明「哪条命令跑过」。
  文案是人手写的、或用别的工具做的，没有 ok 记录；只看 status 会出现「成片都烧好字幕了，
  概览里文案还是灰的，交付自检报还没完成」。产物才是证据，推断出来的会标 `status_from: artifacts`。
- **文件浏览关在笼子里**：请求的路径 `realpath` 必须落在档案区或项目根之内，越界一律拒绝。
- **视频要能拖动**：`/api/raw` 实现了 HTTP Range（206），只返回整段的播放器拖不动进度条。
- **交付前自检要实测分辨率**：`/api/stage?stage=交付` 会用 `ffmpeg -i`（**不是 ffprobe，
  这台机器上那个是坏文件**）读成片尺寸，和目标 `video.mp` 比。
  低于 75% 就判定为「快速档试跑，不是交付件」—— 光看「文件齐、验收 ok」会得出错误结论，
  S05 就是 0.522MP vs 目标 1.26MP 的试跑档。
- **服务进程要自己灌 `config/llm.env`**：它不是登录 shell，不会自动 `set -a && . ...`。
  不灌的话凡是要 key 的活都报 `MISSING_KEY`，看着像「key 没配」，其实只是没人给它（`load_llm_env()`）。

---

## 二、接新客户：先建档，再开工

```bash
V="python3 bin/vstudio.py"

# 1 建档（建 01~08 目录树 + card.json，并登记进总册）
$V card-new --name 某某品牌 --type 客户 --tag M --role "口播·横屏" --aliases "老王,王总"

# 2 填技术真值：录 8 秒人声 / 写逐字稿 / 定起始静帧 → 填进 ip.json 三处 null
$V guard 老王            # 三处齐了才会通过

# 3 随时记习惯和需求（客户说什么偏好，当场记）
$V card-note 老王 "字幕要用品牌蓝" --kind habit
$V card-note 老王 "每周三要两条" --kind need --key 出片频率
$V card-note 老王 "不许出现竞品名" --kind redline

# 4 开工
$V job-new 老王 "M01_开业第一条"
```

**`--aliases` 不是可选项**：你口头叫的是人名（老王），目录名是品牌名。
没有别名，按口头名字搜不到已有档案 → 又建一个重复的。

### 实测过的完整接单动线（2026-10-28）

```bash
# ① 建档（--space 要显式给，否则 newip.py 会卡住等交互输入）
$ vstudio card-new --name 演示客户老王 --type 客户 --tag M \
      --role "本地生活口播·竖屏" --aliases "老王,王总,老王家" --space 老王资产
✓ 建档 演示客户老王  [客户]（已补登记进总册）

# ② 立刻自检 —— 缺真值，直接拒绝开工
$ vstudio guard 老王
  ？ voice.ref_audio        未登记（null）
  ？ visual.still           未登记（null）
✗ 演示客户老王 素材未就绪（4 项），不能出片        ← 退出码 1

# ③ 录 8 秒人声 / 写逐字稿 / 定起始静帧 → 填进 ip.json
$ vstudio guard 老王
✓ 演示客户老王 素材齐备，可以出片                  ← 退出码 0

# ④ 记习惯与红线
$ vstudio card-note 老王 "字幕一律用品牌蓝 #1A5FA8" --kind habit
$ vstudio card-note 老王 "不许出现同行品牌名" --kind redline
$ vstudio card-note 老王 "每周三交付两条" --kind need --key 出片频率

# ⑤ 开工
$ vstudio job-new 老王 "M01_开业第一条"
```

客户卡的 `policy.allow_cloud_llm` 是 **false**（内容不上云，走本地 9B），
本人 IP 才是 true。验证：`vstudio card-show 老王` 最后一行会写明。

模板见 `templates/card.template.json`。

⚠ **客户档案默认禁止内容上云**（`card.json` 里 `policy.allow_cloud_llm: false`），
文案走本地 9B。本人 IP 是 `true`。

### 这条规矩现在由代码执行，不再只是文档（2026-10-29）

以前 `allow_cloud_llm` 只在 RUNBOOK 里写着，**没有任何代码读它** ——
`studio_ai.py` 和 `prompts.py` 直接起 `LLMRouter(sensitive=True)` 就发了。
这里有个很容易混的点：`sensitive=True` 只保证「不进免费池」，
**不等于「不出这台机器」**。于是演示客户的整份人设和台词，真的被送到了云端模型。

现在统一走 `bin/ip_policy.py`：

| 情况 | 结果 |
|---|---|
| 卡里 `allow_cloud_llm: true` | 放行，日志打一行「内容上云：卡里写明允许上云」 |
| 卡里 `false` / 没这个键 / **读不到 card.json** | 拦住，`exit≠0`，报错里写清三条出路 |
| 设了 `STUDIO_ALLOW_CLOUD=1` | 放行，但日志明确写「这次内容会出本机」——**偷偷放行比拒绝更危险** |

闸门位置：`studio_ai.suggest_topics`（人设+需求+红线一起送）、
`prompts.do_gen`（整段台词送）。`card-draft` 是新客户还没有卡，按「用户主动提交」放行，
结果里带 `cloud_policy` 字段说明。

默认 **fail closed**：宁可让 AI 功能报错停下，也不要把客户的商业内容送出本机。

**演示户是例外**：`演示客户老王` 的人物和文案都是虚构的，卡里明确写了允许上云
（`_policy_note` 里注明「不涉及任何真实客户」）。真实客户别照抄这条。

### LLM 路由：按「能力」选通道，而不是按模型名

以前要记住「智谱不能看图、硅基某个 VL 能看图」，写在人脑子里，人一走就丢。
现在能力写在 `config/providers.json` 里，代码只读它：

```bash
set -a && . config/llm.env && set +a

python3 bin/llm_router.py status          # 看各家额度、能力(视觉)、付费/免费池
python3 bin/llm_router.py models          # 核对手写能力与上游实际声明是否还对得上
python3 bin/llm_router.py vision "这帧字幕有没有压边？" --image 帧.png
```

两个新参数：

| 参数 | 默认 | 作用 |
|---|---|---|
| `need="vision"` | 无 | 只在**支持该能力**的通道里挑。写 `text`/`image` 也行 |
| `sensitive` | **True** | True = 这段内容属于客户/商业信息 → **自动跳过第三方免费池** |

⚠ `sensitive=False` 只用于**不含客户信息**的场合（视觉自检、草稿）。
客户台词、IP 档案、出片提示词**一律不许传 False**——免费池的官方说明就写着
「提示词可能被上游记录」。

路由**不做静默降级**：付费视觉全挂时，涉密请求会直接报错而不是悄悄落到免费池。
宁可跑不动，也不能把客户的片子喂给第三方。

```python
from llm_router import LLMRouter
r = LLMRouter()
r.chat(msgs, need="vision")                    # 客户成片 → 只走付费 VL
r.chat(msgs, need="vision", sensitive=False)   # 自己的自检 → 可以用免费 VL
```

当前: 付费 VL 走硅基 `Qwen/Qwen3-VL-8B-Instruct`（实测 1.1s，几分钱），
腾讯混元视觉做厂商冗余；免费 VL 兜底用本地 zen-gate 网关（`:8787`）。

---

### 换人 / 换环境 / 换配乐：改 ip.json，不要改脚本

以前这套东西写在 `gen_video.py` 里（外貌、衣领、背景绿植、88BPM 吉他），
换一个客户就得复制一份脚本再手改 → 20~50 个客户时改一个 bug 要改 50 处。

现在 IP 的所有**名词**都在 `ip.json` 的 `video.prompt_vars`（24 个占位符，
同名对应 `gen_video.py` 里的 `DEFAULT_PROMPT_VARS`）：

```bash
vstudio card-show 洁斌 --json | python3 -c "import json,sys;print(json.load(sys.stdin))"
# 改 ip.json 里 video.prompt_vars 的任意一项 = 换人/换环境/换配乐
```

⚠ **引擎里的默认值必须保持中性**（不写具体的人/衣/物，一律把描述权交回 `<Picture 1>`）。
否则新 IP 还没配 `prompt_vars` 时，这些词会**顶替静帧成为画面的描述来源**——
等于把上一个客户的外貌和陈设灌给下一个客户。这是身份污染，比画质坏严重得多。

引擎脚本只有一份真源：`templates/scripts/`（25 个脚本 + manifest）。

```bash
vstudio scripts-sync 老王            # 先看 diff
vstudio scripts-sync 老王 --force    # 覆盖（旧文件进 08_过程/_脚本备份/时间戳/）
vstudio scripts-diff                 # 各 IP 谁跑偏了
```

某个 IP 若真需要独占逻辑，先问「能不能变成 ip.json 的一个参数」——
能变成参数才是正解，直接改脚本等于重新引入分叉。

---

## 三、出一套片的完整动线

阶段固定：**文案 → 音频 → 视频 → 后期 → 字幕 → 验收 → 交付**

```bash
V="python3 bin/vstudio.py"

$V guard 洁斌                                   # ✗ 就停下，别硬跑
$V job-new 洁斌 "S05_讲AI变现"                    # 建 05~08 四个目录 + job.json
$V job-set 洁斌 S05 文案 ok --artifact 05_文案/S05_讲AI变现/S05_文案_定稿.md
$V job-set 洁斌 S05 音频 ok
$V job-set 洁斌 S05 视频 ok
$V job-set 洁斌 S05 后期 ok
$V job-set 洁斌 S05 字幕 ok
$V job-set 洁斌 S05 验收 ok --note "接缝/油光/字幕同步 全过"
# 产物拷进 profiles/<IP>/资产/ 后，交付自动识别
$V job-show 洁斌 S05
```

**每完成一步就 `job-set` 一次**——这是防「隔一周回来不知道做到哪」的唯一办法。

---

## 三之二、一条龙出片（pipeline）

上面那套 `job-set` 是**人工推进**用的；真出片用编排器，一条命令跑完 6~10 步。

```bash
V="python3 bin/vstudio.py"

# ① 先算参数看看（不跑任何东西）
$V algo 洁斌 S05                       # 片长/帧数/段数/留白，按文案字数估算
$V algo 洁斌 S05 --mp 0.5 --segs 4     # 换档位、锁段数再算一遍

# ② 提示词：AI 生成 → 过红线 → 落盘
$V prompts 洁斌 S05 --gen              # 生成（未过红线则整批不落，被拒内容存 08_过程）
$V prompts 洁斌 S05 --optimize         # 在现有基础上优化（旧版进 history）
$V prompts 洁斌 S05 --set 2 --text "…" # 手动微调第 2 段（同样过红线）
$V prompts 洁斌 S05                    # 看当前提示词

# ③ 跑流水线
$V pipeline 洁斌 S05 --tier fast       # 低分辨率试跑（544x960，约 5 分钟）
$V pipeline 洁斌 S05 --tier std        # 正式档（864x1536）。实测 28.5s 的片 video 段 841s，
                                       # 全链 tts→audit 约 14 分钟；片长按段数往上加
$V pipeline 洁斌 S05 --mode step       # 每步确认（回车/s 跳过/q 退出）
$V pipeline 洁斌 S05 --from video      # 从某步接着跑（断点续跑）
$V pipeline 洁斌 S05 --dry             # 只打印命令，不执行
```

**断点续跑**靠"产物在不在"判断，不靠记忆：产物已存在就跳过，加 `--force` 才重跑。
每跑一次在 `08_过程/<套次>/pipeline-<时间>.json` 留一份日志（命令/耗时/产物/退出码）。

**档位**（`config/pipeline.json` 的 `tiers`）：

| 档位 | 分辨率 | 用途 |
|---|---|---|
| `fast` | 544x960 (0.5MP) | 验提示词/分段/口型。**不能**验清晰度与曝光 |
| `pilot` | 864x1536 | 只出第 1 段，正式分辨率验画面细节 |
| `std` | 864x1536 | ★ 正式出片 |
| `high` | 864x1536 | 留位（后续接二采/长片参数） |

**自适应**（`bin/studio_algo.py`，算法已抽回引擎，IP 只留参数）：
- 总帧数吸附到 `5+17n`（H3 采样器硬约束）
- 单段上限按**实际渲染面积**反比算（1.26MP → 141 帧；0.5MP → 192 帧）
- 段数按目标段长（默认 8s）自适应，切点天生落在 17 的倍数上
- 音频前后自动留白（前 ~0.28s / 后 ~0.56s，随片长浮动）——**SRT 是在留白后的音频上做的，
  所以字幕时间轴自动带上偏移，不用再挪**
- 配乐**比人声低多少 LU**（<45s：13 LU / >45s：15 / >90s：17）——
  注意这是**相对关系**，不是固定 dB。绝对增益由 `mix_bgm.py` 实测两边响度后反算，
  再叠一层 sidechain 鸭子压缩（人声一响配乐自动退 6~9 dB）。
  旧写法固定 −6 dB 的前提是"所有配乐响度差不多"，S05 就翻车了：
  配乐 −12.3 LUFS、人声 −27.7 LUFS，垫完配乐还压人声 9 LU → 用户听到"BGM 大声、人声小"。

流程定义全在 `config/pipeline.json`，**改它就是改流程**，改完先 `--dry` 跑一次。

---

## 三之三、模型链与工作流弹性（2026-10-28 定板）

H3 的模型配置**只写在档案卡**（`ip.json` 的 `video.*`），`gen_video.py` 只是读它：

| 字段 | 现值（陈洁斌主号） | 说明 |
|---|---|---|
| `video.unet` | `Minimax-h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | ref2va 底座 |
| `video.clip` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 与 ref2va 配套，不能配 nvfp4 |
| `video.lora.name` | `minimax_h3/dmad_minimax_h3_4step_full_critic_comfyui.safetensors` | ⚠ 是 **dmad_** 不是 dmd_ |
| `video.lora.strength` | `1.0` | 写 `none` 就整个跳过 LoRA 节点 |
| `video.sage_attention` | `auto` | 节点类名是 `PathchSageAttentionKJ`（源码就拼错） |
| `video.steps` | `8` | BasicScheduler simple / denoise 1.0 |

**节点顺序必须是 `UNET → LoRA → Sage → 低显存分块 → (调度器 + 采样器)`**：
LoRA 改权重所以紧跟 UNET；Sage 改 attention 实现；低显存分块必须在最外层，
否则它切出来的 attention 会被 Sage 覆盖，分块白挂、显存照样爆。
**调度器和采样器必须吃同一个 model 引用**，只给采样器挂补丁 = 补丁不生效。

### 换人 / 换背景 / 换台词（用户要的方向）

这条链本质是「参考图 + 锁定音频 + 时间轴」驱动，所以三种业务都是改参数不是改代码：

| 业务 | 改哪里 | 不用动代码 |
|---|---|---|
| 换台词 | `05_文案/<套次>/*.txt` 改完重跑 `tts → srt → video` | ✓ |
| 换背景 | 换 `02_素材真值/` 的静帧（`ip.json` 的 `visual.still`） | ✓ |
| 换人 | 换静帧，或在 `video.graph_overrides` 里把 84 号节点的 `lora_name` 换成 `minimax_h3_head_swap_v1.0_r32` | ✓ |

`video.graph_overrides` 是给后续新工作流留的口子：`{"节点id": {"输入名": 值}}`
会直接合并进提交给 ComfyUI 的 graph。引擎不认识具体业务，只负责合并。
节点 id：`82`UNET `84`LoRA `85`Sage `83`低显存 `9`调度器 `30`采样器 `47`分辨率。

---

## 三之四、这四个坑都真踩过（别再花同样的时间）

1. **长任务被"挂起"而不是"卡死"**：跑超过前台超时的命令，外层会把整个进程组挂起，
   子进程（ffmpeg / python）全部停在 `STAT=T`（`WCHN=do_signal_stop`），
   CPU 0%、没有任何报错 —— 看起来像死锁，其实是进程被 SIGTSTP 按了暂停键。
   **判据：`ps -o stat` 是 `T` 就 kill -CONT 再 kill，别去查 I/O。**
   对策：媒体类长命令（出片/混音/验收抽帧）**一开始就用后台方式启动**；
   脚本里仍然要加 `-nostdin`（非交互环境下 ffmpeg 读 stdin 本来就没意义，
   加了少一类隐患）。
2. **`DRY=0` 也会干跑**：`os.environ.get("DRY")` 对字符串 `"0"` 是真值 →
   只打印提示词、不出片、还返回 0（看着像成功）。判据已统一成
   `not in ("", "0", "false", "off", "no")`。
3. **失败步骤会留 0 字节空壳**：ffmpeg 失败前先建输出文件，下一轮断点续跑
   看到"文件在"就跳过，然后拿空壳去烧字幕。现在 `newest()` 忽略 0 字节，
   失败时还会清掉本步时间窗内生成的半成品。
4. **`--from` 断点续跑只认"跑过的"，不认"已经在的"**：从 `mix` 接手时前面的
   `graded` 变量没人赋值 → 报"模板变量未解析"。现在启动时按 `out_glob` 回填
   （文件系统才是真相来源）。

---

## 四、让我和其他 AI 调用

**方式 A**：我直接用 Bash 调 CLI，加 `--json` 拿结构化结果。零配置，现在就能用。

**方式 B**：挂成 MCP（已写进 `.mcp.json` / `.cursor/mcp.json`）
```json
"ip-studio": {
  "command": "/home/qmr/github/ai-video-studio/mcp-servers/ip-studio/.venv/bin/python",
  "args": ["/home/qmr/github/ai-video-studio/bin/mcp_server.py"]
}
```
11 个工具：`cards` `card_show` `card_note` `ips` `jobs` `job_show` `job_new`
`job_set` `guard` `scripts` `status`

**方式 C**：HTTP `:8000`（n8n 用）

**先自检再接**（别靠「文件在就算能用」判断）：

```bash
./mcp-servers/ip-studio/.venv/bin/python bin/mcp_probe.py
# ✓ 握手成功  服务 ip-studio 1.30.0
# ✓ 工具 11 个：ips, jobs, …
# ✓ 实调 status() 返回了内容
```

`mcp_probe.py` 真走一遍 `initialize → tools/list → tools/call`。
只看文件存不存在没用：mcp 包升级会改 `FastMCP` 的导入路径（2.x 改名
`MCPServer`），工具签名写错会在握手阶段就抛 —— 这些只有真握手才暴露。
界面顶栏那个「MCP 接口」圆点跑的就是它。

**方式 D**：工作台 HTTP（`bin/ui_server.py`，给网页用，不建议外部系统直连）

---

## 五、所有命令

| 命令 | 作用 |
|---|---|
| `cards` | 列出所有档案卡（类型/角色/状态/套数） |
| `card-new --name … --type 本人IP\|客户 --role … --aliases …` | 建档 |
| `card-show <ip>` | 看需求/习惯/红线/数据策略 |
| `card-note <ip> "…" [--kind habit\|need\|redline] [--key 字段名]` | 记一条 |
| `jobs [ip] [--only 关键字]` | 套次进度 |
| `job-new <ip> <Sxx_主题>` | 开一套 |
| `job-show <ip> <Sxx>` | 一套的详情 |
| `job-set <ip> <Sxx> <阶段> [ok/fail] [--artifact 路径] [--note 备注]` | 推进 |
| `guard [ip]` | 开工自检（缺素材非零退出） |
| `pipeline <ip> <套次> [--tier fast\|std\|pilot\|high] [--mode auto\|step] [--from X] [--to Y] [--dry] [--force] [--segs N] [--grid 9,10,7]` | 一条龙出片（见三之二） |
| `prompts <ip> <套次> [--gen\|--optimize\|--show] [--set N --text "…"] [--dry]` | 提示词工作台（AI 生成/优化/微调，过红线才落盘） |
| `algo <ip> <套次> [--sec 秒] [--mp 1.26] [--segs N]` | 自适应参数预览（帧数/段数/留白/配乐） |
| `scripts-diff [--base IP]` | 各 IP 脚本偏离比对（谁改了什么、哪些能抽回引擎） |
| `scripts [ip]` | 该 IP 能跑的生产脚本 |
| `scripts-sync <ip> [--file a.py,b.py] [--force]` | 把引擎真源推给某 IP（默认只预览，`--force` 才写，旧文件自动备份） |
| `llm status\|chat\|models\|vision --image 帧.png "…"` | LLM 路由（见第二节） |
| `index [--write]` | 重建索引 / 给旧套次补 job.json |
| `status` | 总览（卡 + 套次 + 三个服务） |

另有：**`bin/ui_server.py`** —— 本地网页工作台（见「一之二」），不是 vstudio 子命令，单独起进程。

全局加 `--json`：输出 JSON，给 agent 解析。
`ip` 参数写名字/别名都行（"洁斌"、"主号"、"老王"）。

---

## 六、隔离保证

- 珠珠（雅太社）是**独立体系**，`vstudio` 硬拦截：
  `✗ 「珠珠」属于独立体系…本 CLI 不碰。`
- 每个 IP 一份脚本副本（参数早已分叉：采样器/二采模型/画幅/字号），共用必串参数。
- 每个 IP 自己的 `资产/`，客户之间互相看不见。
- 客户内容默认不上云（走本地 9B）；本人 IP 可走云端。

---

## 七、版本管理：两个库，各管各的（2026-10-28）

项目原本**没有 git**——引擎改坏了没法回滚，与「两边分家」的诉求直接冲突。
现在**两个 git 库**，对应两条改动线：

| 库 | 位置 | 管什么 | 远程 |
|---|---|---|---|
| **引擎库** | `/home/qmr/github/ai-video-studio/` | 代码/文档/模板/总册 | ✅ 可推 GitHub（已配 `origin`） |
| **档案库** | `/cfu/ai视频档案库/` | 脚本副本、card.json、ip.json、文案、制作记录 | ⛔ **不设远程**（含客户商业信息） |

```bash
# 引擎侧
cd /home/qmr/github/ai-video-studio && git log --oneline

# 档案侧（脚本改坏了能回滚，这是 S05 抓到 calm_expr bug 后的补丁）
cd /cfu/ai视频档案库 && git log --oneline
```

**两边都只收文本**。成片/音频/图片/字体/交付件靠文件系统与盘级备份管，不进 git
—— 引擎库 1.5M，档案库 .git 仅 572K，都不会因为出片越来越多而膨胀。

⚠ 档案库在 NTFS 上，**必须** `core.filemode=false` + `core.autocrlf=false`，
否则 git 会把所有文件判成已修改（NTFS 没有 unix 权限位）。已配好。

⚠ `OpenMontage/` 有自己的 `.git`，已从引擎库排除（否则会被当 gitlink，clone 后内容丢失）。

### 推 GitHub 之前

```bash
git remote -v     # origin → https://github.com/ZMZ913141/ai-video-studio.git
```
1. 先在 GitHub 上建 `ai-video-studio` 空仓库（别勾 README/LICENSE，本地已有）
2. 配凭证：HTTPS 用 Personal Access Token，或改用 SSH
   `git remote set-url origin git@github.com:ZMZ913141/ai-video-studio.git`
3. 推之前再确认一次密钥没进去：`git ls-files | grep -i env` 只应出现 `*.example`

## 八、位置变更沿革（别再走回头路）

```
/cfu/洁斌资产/陈洁斌主号        ← 最初的现场（已废弃）
   ↓ 2026-10-28 迁入项目
ai-video-studio/profiles/陈洁斌主号
   ↓ 2026-10-28 晚 档案再迁出，独立存储区（当前）
/cfu/ai视频档案库/陈洁斌主号     ← ★ 现在在这里
```

- `/cfu/洁斌资产/` 已整体移入 `/cfu/废品站/2026-10-28_洁斌资产_旧位置_已全量迁移/`，
  **不是删除**，翻旧账随时可取回（移前 `diff -rq` 核对过：133 文件除 `gen_video.py`
  外完全一致，那处差异正是 S05 试跑修掉的否定词 bug）。
- 迁移前抢救了两份**不属于档案树但有用**的东西：

  | 文件 | 新位置 | 为什么不能丢 |
  |---|---|---|
  | `多IP协作规范.md` | `docs/多IP协作规范.md` | 接客户时的隔离规矩，md5 校验一致 |
  | `ai-workbench.html` | `ui/ai-workbench.html` | 1452 行 UI 草稿，以后做看板的底子 |

⚠ 若你有外部工具（Kdenlive 工程、剪映项目）引用 `/cfu/洁斌资产/...` 的绝对路径，
那些引用需要手动改指到 `/cfu/ai视频档案库/...`。
