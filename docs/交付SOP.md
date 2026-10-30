# 交付 SOP · 一条片子从 0 到能发

> 这份写的是**动作顺序**，不是原理。原理看 `docs/多IP协作规范.md` 和
> `docs/H3时间轴与分段素材规范.md`。所有命令都在引擎目录 `/home/qmr/github/ai-video-studio` 下执行。

---

## 〇、先定档位——这是后面一切的前提

段数是**算出来的，不是文案决定的**。同一份文案换档位，段数会变：

| 档位 | 分辨率 | 用途 | 20s 文案大概几段 |
|---|---|---|---|
| `fast` | 544×960（0.5MP） | 只验提示词/分段/口型/接缝，**不能验清晰度和皮肤** | 3 |
| `pilot` | 864×1536（1.26MP），只出第 1 段 | 验画面细节与身份一致性 | 1 |
| `std` | 864×1536（1.26MP） | **正式出片** | 4 |
| `high` | 同 std | 预留 | — |

```bash
python3 bin/vstudio.py algo 陈洁斌主号 S02 --tier fast   # 先看这个档位算出来几段、切在哪
```

⚠️ **档位决定段数 → 段数决定提示词。** 先跑 `algo` 看段数，再去生成提示词。
顺序反了，会出现「提示词 4 段、出片 3 段」——第 4 段白写，缺的段静默退回默认描述。

---

## 一、标准动作序列

### 1. 写文案（或改已有文案）

```bash
# 素材位：05_文案/<套次>/  里放 job.json + S02_字幕文本.txt + landing.md
```

文案定长后**不要急着出片**，先看它多长、分几段：

```bash
python3 bin/vstudio.py algo 陈洁斌主号 S02 --tier std
```

### 2. 生成提示词（中文分镜）

```bash
python3 bin/vstudio.py prompts 陈洁斌主号 S02 --gen --tier std   # 按 std 的段数切
python3 bin/vstudio.py prompts 陈洁斌主号 S02 --to-en --force    # 译成英文（出片只认英文）
```

界面里同一个动作：提示词页 → 选档位 → 「AI 生成」→「译成英文」。

### 3. 素材清单（只在要换脸/换服装/加参考视频时才需要）

`05_文案/<套次>/materials.json`：

```json
{
  "segments": { "1": ["img"], "2": ["img"], "3": ["img"] },
  "videos":   [{ "id": "v1", "file": "02_素材真值/<套次>/xxx.mp4", "use": "edit", "trimStart": 0 }],
  "seg_videos": { "2": ["v1"] }
}
```

规则（血泪版）：
- 每段都要有兜底 `["img"]`，否则那一段没有参考图 → 脸崩
- 换人/换服装用 `use: "edit"` 且**必须把自己静帧勾到那段**；`guide` 会硬锁原人物
- 每段 ≤3 个视频、总时长 ≤15s、图+视频+音频合计 ≤12，**超了插件静默丢**
- 视频时间窗跟着段走，改文案不用重对齐；只换内容用 `trimStart`

### 4. 真跑

```bash
python3 bin/vstudio.py pipeline 陈洁斌主号 S02 --tier fast            # 试跑
python3 bin/vstudio.py pipeline 陈洁斌主号 S02                        # 正式（std）
python3 bin/vstudio.py pipeline 陈洁斌主号 S02 --from ass             # 从某步接着跑
```

界面：套次页「跑全流程」。选「只重做配音」这类单步动作时，走的是同一条拼装逻辑。

### 5. 验收（**必做，跳过 = 交半成品**）

```bash
# ① 抽三帧目视：开头/中间/结尾
ffmpeg -y -ss 3  -i 07_成片/<套次>/<套次>_成片_字幕版_*.mp4 -frames:v 1 /tmp/f1.png
ffmpeg -y -ss 10 -i 07_成片/<套次>/<套次>_成片_字幕版_*.mp4 -frames:v 1 /tmp/f2.png
ffmpeg -y -ss 18 -i 07_成片/<套次>/<套次>_成片_字幕版_*.mp4 -frames:v 1 /tmp/f3.png

# ② 字幕与音频同步审计（唯一可信的字幕验收）
python3 04_验收工具/srt_sync_audit.py ...

# ③ 片长/流/音量
ffmpeg -i <成片> 2>&1 | grep -E "Duration|Stream"
```

抽帧要看四件事：**人物是否同一人 / 口型是否在念 / 字幕位置字号是否对 / 结尾 CTA 在不在**。

---

## 二、出片前必查三件事

1. **提示词段数 = 出片段数**
   出片日志第一行会打印 `音频 xx s → 输出 N 帧 / N 段`，
   紧接着若出现 `⚠ 英文分镜段数对不上` → 立刻停，回去按当前档位重生成提示词。
2. **内存够**
   ```bash
   grep MemAvailable /proc/meminfo       # 低于 ~8G 别提交，会被内核 OOM 杀掉，GPU 时间全废
   ```
3. **ComfyUI 活着**
   ```bash
   curl -s --noproxy '*' -m 3 http://127.0.0.1:8188/system_stats -o /dev/null -w "%{http_code}\n"
   bash bin/comfy_supervisor.sh status     # 更全：守护在不在、重启过几次、上次怎么死的
   ```
   ★ 流水线自己也会查（`pipeline.json` 里 tts/srt/video 三步标了 `needs_comfy`），
   服务不在就**开工前**停住并给恢复命令，不会跑十步死在半路。

---

## 二·五、ComfyUI 挂了怎么办 —— 三层防线

### 先说结论：它一定会再挂，所以别指望"不挂"

2026-10-30 真崩了一次，是这类事故的标准长相：

```
05:22:39  出完最后一条视频（WAN 视频模型还占着显存）
05:23:44  批量任务紧接着提交 TTS（要换成语音模型）
05:24:10  内核：NVRM: Xid (PCI:0000:01:00): 43, pid=8910, name=python
          → python 进程当场消失，没有 Traceback，日志断在半句
05:24→05:36  流水线还在原地轮询一个永远不会来的结果（白等 12 分钟）
```

**`rc=0` 的日志是查不出这种事的**——进程被内核/驱动干掉时一个字节都写不出来。
判读必须看内核：

```bash
journalctl -k --since "-30min" | grep -iE "Xid|out of memory|Killed process|NVRM"
```

### 死法对照表（对着这张表看，别猜）

| 证据 | 死法 | 处置 |
|---|---|---|
| `NVRM: Xid ... 43`（或 13/31/79） | **GPU 层故障**，最常见于**换模型那一瞬**（视频模型 → TTS 模型） | 用现在的 `run.sh`（已去掉 `--high-ram`、async 流 2→1）；批量时别让「出片」和「TTS」紧挨着抢显存 |
| 退出码 137 + `Killed process … anon-rss` | **内核 OOM**（RAM 不足，不是显存） | `python3 bin/comfy_guard.py oom`，再 `preflight`；别并发跑两条 |
| 退出码 129 | **SIGHUP**：启动它的终端被关了 | 别再手敲 `./run.sh` 放前台，用守护/服务 |
| 退出码 143 | SIGTERM，被正常终止（有人 stop / 注销） | 正常，重启即可 |
| 内核日志**完全没记录** | 外部 `kill` 或终端关闭 | 同上，改用守护 |

### 三层防线（已全部落地，按"能不能接受一次损失"排序）

**第一层：别崩** — `/cfu/ComfyUI/run.sh` 已改成稳定性优先

```bash
COMFY_HIGH_RAM=0     # 不让旧模型赖着不卸载（换模型才干净）
COMFY_ASYNC=1        # 异步卸载流 2 → 1，暂存减半
COMFY_RESERVE_VRAM=1.0   # 给桌面/系统留 1G，0.5 太抠
```
想回高性能：`COMFY_HIGH_RAM=1 COMFY_ASYNC=2 ./run.sh`（会更快，也更容易再崩）。
原文件留在 `run.sh.bak-20261030-xid43`。

**第二层：崩了自己回来** — 守护进程 / 用户级服务

```bash
bash bin/comfy_supervisor.sh start     # 后台常驻，关终端也不死；死了 5 秒后自动重启
bash bin/comfy_supervisor.sh status    # 守护在不在 / 服务活没活 / 重启过几次 / 上次死因
bash bin/comfy_supervisor.sh logs      # 跟 ComfyUI 输出
```
每次死亡它都会把**退出码 + 内核 Xid/OOM + 该从哪一步续跑**写进
`.cache/comfy/supervisor.log`。这就是"死因取证"。

想彻底不用管（开机就在、崩溃自愈）：

```bash
bash bin/install-comfy-service.sh             # 装成 systemd 用户服务（一次）
sudo loginctl enable-linger $USER             # 可选：开机不用登录也起
systemctl --user status comfyui               # 之后只认这一条
```

**第三层：崩了不白等** — 流水线侧

- 开工前门禁：`tts / srt / video` 标了 `needs_comfy`，服务不在就**先停**并打印恢复命令
- 提交/轮询都会快速失败（连续连不上约 1 分钟就报错退出，不再无限重试）
- 心跳：等超过 1 分钟会打印一行，人能看出"它在排队"还是"它在等死"

### 为什么「AI 自己把 ComfyUI 起回来」做不到

不是不肯，是这个执行环境拿不到显卡，实测四条路全堵：

| 路径 | 实测结果 |
|---|---|
| 直接 `run.sh` / `python main.py` | `torch.cuda.is_available() = False`、`devices 0`、`Can't initialize NVML`（seccomp 挡 CUDA 驱动调用）→ 起个没显卡的空壳，提交任务必失败，比不起更误事 |
| `systemctl --user` | `Failed to connect to user scope bus`（总线不可达） |
| `crontab` / `at` | `crontabs/root/: 权限不够` |
| `ssh localhost` 绕出沙箱 | sshd 没监听（`Connection refused`） |

所以守护脚本**先自检 GPU 再决定要不要起**：拿不到就直接说清楚，不会偷偷起个残废服务。
你只需要在自己的终端执行一次 `bash bin/comfy_supervisor.sh start`（或装成服务），
之后它自己负责所有重启——**这才是"确保它不挂"的真正形态**。

---

## 三、断点续跑 / 重做的真实语义

- 「产物在」**不等于**「产物还有效」。上游变了，下游按 `depends` 自动重做。
- 人改过的源文件（`script.txt`、`materials.json`）要在 `inputs` 里声明，否则改了也一路跳过，交出去还是旧片。
- `--from X` 的语义是**从 X 开始重做**（X 本身强制重做），不是「从 X 开始判断跳过」。
- 种子是重跑语义的一部分：显式 `--seed` 变了会判定为「变了」→ 重出。

---

## 四、已知坑（都是真跑撞出来的，别再踩）

| 现象 | 真实原因 | 处置 |
|---|---|---|
| 字幕步骤失败：`字号 52 太大：12 字要 465px，安全区只有 438px` | 字号按 768×1376 定板，换成 864×1536 没缩放 | 已修：按 `ip.json` 的 `base_res` 几何平均缩放；超了自动降号不中断（要严格失败设 `STRICT_FONT=1`） |
| 点「只重做配音」没反应，听不到新语气 | `--from` 只挪遍历起点，产物在就跳过 | 已修：`--from X` 强制重做 X |
| 出 3 段只有 2 段分镜，末段画面跟台词对不上 | 提示词按「45 字」硬切，跟出片用的分段是两套算法 | 已修：两侧统一走 `studio_algo.plan()`；先定档位再生成提示词 |
| 全片表情/动作跟台词差半句，但日志全绿 | `prompts.en.json` 段号 `i` 从 **1** 起，脚本循环下标从 **0** 起 → 整份分镜错位一段 | 已修：取分镜时 `+1`；`i` 出现 0 直接拒绝出片 |
| 每次跑都报「时长偏离预估 +2.8s」 | 去标点后的字数拿含标点的语速（3.85）去除，必然低估 14% | 已修：口径统一 |
| 界面显示 `--gen`，实际按 fast 跑；非法档位也返回成功 | 命令行预览与执行器各写一套 | 已修：预览优先走执行器同一份拼装 |
| 手工设 `TARGET_SECS=8` 想「每段 8 秒」，结果出了 8 段 | 这个变量是**目标段数**，不是秒数 | 已改名 `TARGET_SEGS`（老名仍兼容，会打一行提示） |
| 成片字幕出现**双层重影** / 画面顶部莫名多出文字 | 参考视频本身带字（烧过字幕的成片、水印、台标）。模型会照着参考画面**自己重画文字**，位置还可能跑到顶部 | 换**原始无字**素材重出；出片日志会提醒 |
| TTS/出片**卡十几分钟一个字不打** | ComfyUI 中途崩了（先查内存：还有几十 G 就不是 OOM，就是服务没了） | 已修：开工前门禁 + 轮询连续失败即报错退出；服务用守护进程托管后自己会回来（见「二·五」） |
| 出片/配音跑到一半服务没了，`system_stats` 返回 **000** | 被 GPU 判死（`Xid 43`，换模型瞬间）或终端被关（`SIGHUP`） | 用 `bin/comfy_supervisor.sh start` 托管；死因看 `.cache/comfy/supervisor.log` |
| 启动时刷一串 `IMPORT FAILED`（某几个插件报缺依赖） | **多半不是插件坏了**：是同时起了两个 ComfyUI，抢不到 8188 的那个顺手报一串假错然后退出（2026-10-30 用户看到的 SeedVR2 就是这种；活的实例其实把它的 6 个节点都加载了） | `ps -eo args \| grep main.py` 看是不是两个；守护已加**单实例锁 + 端口占用即接管**；别再手敲 `./run.sh`，只用 `comfy_supervisor.sh start` |

---

## 五、界面上「看起来成功」的假象

- 子进程非零退出也是失败，**不能只看有没有抛异常**，要看回执。
- 阶段状态以**产物文件**为准，不以日志最后一行“✓”为准。
- `confirmBox()` 会换掉 `#modalBox`：先取值 → 再确认 → 再画进行中。
- `innerHTML=` 之后旧引用失效，点击前重新 `getElementById`。

---

## 六、清理与留档

- 一律不用 `rm`。废弃文件进 `/cfu/废品站/<日期>_<内容>_<原因>/`，留一份 `md5` 清单。
- 脚本改完从 `templates/scripts/` 同步到档案：`python3 bin/vstudio.py scripts-sync <IP> --force`
  （IP 侧是副本，不同步＝白改）。
- 改了影响出片的东西，先存 golden（`DRY=1` + `DUMP_TIMELINE=1`），再 diff。
