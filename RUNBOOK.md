# 运行手册（无 UI 版）

> **原则：界面是可选项，接口才是本体。**
> 三个入口，同一批底层函数，能力完全相同：
>
> | 入口 | 谁用 | 怎么用 |
> |---|---|---|
> | **CLI** | 人、我（WorkBuddy）、任何能跑 Bash 的 agent | `python3 bin/vstudio.py <命令>` |
> | **MCP** `ip-studio` | 任何支持 MCP 的 AI（Claude Code / Cursor / Qwen Code） | 挂上即用，11 个工具 |
> | **HTTP** `:8000` | n8n / 外部系统 | pipeline-service REST |
>
> UI（`ai-workbench.html`）将来只做**这些数据的显示**，不另发明流程。

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
$V pipeline 洁斌 S05 --tier std        # 正式档（864x1536，20 分钟左右）
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
- 配乐增益随片长后退（>45s −7dB，>90s −8dB）

流程定义全在 `config/pipeline.json`，**改它就是改流程**，改完先 `--dry` 跑一次。

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
| `llm "…"` | 走 LLM 路由（6 家自动切换） |
| `index [--write]` | 重建索引 / 给旧套次补 job.json |
| `status` | 总览（卡 + 套次 + 三个服务） |

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
