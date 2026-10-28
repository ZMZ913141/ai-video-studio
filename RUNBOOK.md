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
└── profiles/       ★ 档案卡：一人一张，互不污染
    └── 陈洁斌主号/
        ├── card.json      ← 业务层：需求/习惯/红线/数据策略
        ├── ip.json        ← 技术层：音色/静帧/采样参数（真值）
        ├── 01_人设与规范/  02_素材真值/  03_生产脚本/  04_验收工具/
        ├── 05_文案/  06_音频/  07_成片/  08_过程/
        └── 资产/          交付件（本 IP 私有，别家看不见）
```

**改动分两边，互不干扰**：

| 改什么 | 动哪里 | 影响 |
|---|---|---|
| 引擎升级（新模型、新字幕样式、路由策略） | `bin/` `orchestration/` `captions/` `config/` | 所有 IP 受益，**档案卡一个字都不用改** |
| 个人档案（换音色、改习惯、加红线） | `profiles/<IP>/` | 只影响这个 IP，**引擎不动** |

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

## 七、⚠ 旧位置已废弃

`/cfu/洁斌资产/陈洁斌主号` 是**迁移前的物理备份**，已全量复制到本项目
`profiles/陈洁斌主号/`（133 个文件、530M，已逐项核对）。

**现在的项目真值在新位置**（`registry.json` 只指向新路径），旧目录不会被任何脚本读取。
等你确认新位置一切正常后，旧目录可自行清理（建议 `gio trash`，别用 `rm`）。

⚠ 若你有外部工具（Kdenlive 工程、剪映项目）引用 `/cfu/洁斌资产/...` 的绝对路径，
那些引用需要手动改指向新位置。
