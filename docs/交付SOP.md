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
| `setsid nohup … run` 在沙箱里起**守护**（绕开 CUDA 自检） | 进程 detach 之后连 `/dev/null` 都打不开（`Function not implemented`），`date` / `sleep` 报 `libsystemd.so.0` 找不到 → 守护变成残废：时间戳算不出来、日志刷错误、`存活 -1793325170s` 这种离谱数字就是这么来的 |

所以守护脚本**先自检 GPU 再决定要不要起**：拿不到就直接说清楚，不会偷偷起个残废服务。
你只需要在自己的终端执行一次 `bash bin/comfy_supervisor.sh start`（或装成服务），
之后它自己负责所有重启——**这才是"确保它不挂"的真正形态**。

补一句：**「接管一个已经在跑的服务」不需要 GPU**（已改成先探端口、通了就跳过 GPU 自检），
但从沙箱里 detach 的守护仍然是残废（见上表最后一行）。所以在自己终端敲那一条 `start`
依旧是必须的，只是它会立刻接管你手上那个活着的实例，不会重复启动。

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
| 配音步骤报 `'NoneType' object is not callable`，堆栈落在 **Breeze-TTS** 的 `native.py ... flash_attn_func(...)` | **不是 TTS 的错，是别的插件栽赃**：`ComfyUI-SeedVR2_VideoUpscaler/src/optimization/compatibility.py` 在**模块级**执行 `ensure_flash_attn_safe()`，往 `sys.modules` 塞了个假 `flash_attn`（`flash_attn_func = None`）。TTS 的函数内 `from flash_attn import flash_attn_func` 因此「导入成功但拿到 None」，还在 `ALL_ATTENTION_FUNCTIONS` 注册了这个坏后端，跑到第一层 attention 才炸 | 已修：Breeze-TTS 改用**功能探测**（真的拿到可调用的 `flash_attn_func` 才算可用），而不是信 `importlib.util.find_spec`；注册点再兜一道，探测不过就不注册、整体退回 `sdpa`。**取证口诀：报错在 A 插件，真凶可能在 B 插件；`find_spec` 为真 ≠ 能用** |
| 守护进程不停地重启，`ComfyUI` 的 pid 每十几秒换一个，日志刷 `Port 8188 is already in use` / 一堆假 `IMPORT FAILED` | `start_comfy` 用 `$!` 记 pid，**记到了错误的 pid**（run.sh 之间多一层子 shell）。守护看到 `kill -0 错误pid` 失败 → 以为服务死了 → 再起一个 → 抢不到端口退出 → 循环。健康的老实例反而被晾在一边，`status` 永远显示「pid 不可见」 | 已修：起来后按 **cmdline 反查**真实 pid（`main.py` + 本端口）覆盖 pid 文件；`kill` 前先校验 cmdline，对不上就拒绝（防误杀）。另外 `wait` 拿不到退出码时会返回 `127`，**那不是「命令找不到」**，已在 `forensics` 里注明 |
| 守护明明没在跑，`start` 却说「守护已在运行（锁被占）」 | `exec 9>"$LOCKF"` 加的 `flock` fd **被子进程继承了**：守护自己退出后，ComfyUI 还攥着那把锁 | 已修：起 ComfyUI 的子 shell 里先 `exec 9>&-`。万一又遇到，`for p in /proc/[0-9]*; do ls -l $p/fd \\| grep -q comfy/lock && echo $p; done` 能揪出持有者 |
| 审计/验收步骤卡了几分钟不结束，ComfyUI 那边也没动静 | 队列里卡着**幽灵任务**（客户端被强杀时，已提交的 prompt 还挂在 `queue_running`），后续任务全在排队 | `curl -s --noproxy '*' http://127.0.0.1:8188/queue` 看，然后 `curl -X POST .../interrupt` 清掉。正常审计 21 条约几十秒，跑到 400s 就是被堵了 |
| 启动时刷一串 `IMPORT FAILED`（某几个插件报缺依赖） | **多半不是插件坏了**：是同时起了两个 ComfyUI，抢不到 8188 的那个顺手报一串假错然后退出（2026-10-30 用户看到的 SeedVR2 就是这种；活的实例其实把它的 6 个节点都加载了） | `ps -eo args \| grep main.py` 看是不是两个；守护已加**单实例锁 + 端口占用即接管**；别再手敲 `./run.sh`，只用 `comfy_supervisor.sh start` |
| 听感上 **BGM 把人声盖住了**（音乐和人声差不多响） | 配乐增益是拿**处理链之前**的人声算的，而 `acompressor` 会自己吃掉一大截（T01 实测 −22.7 → −32.9 LUFS，吃 10.2 LU；T02 只吃 2.6、S02 只吃 0.1 —— 全看语音峰值因数）→ 名义「配乐低 13 LU」实际只低 0.9 LU。另一个帮凶：整片 `loudnorm` 放在混音**之后**，会把只剩配乐的停顿段当安静段多补几 dB | 已修：配乐改按**处理之后**的人声反算；归一挪到人声链末尾（只对人声），混完只留 `alimiter`。**验收看「说话段 − 停顿段」的电平差，不看 LUFS**：<8 dB 听不清、<12 开始糊、专业口播 15~20 dB。`audio_check.py` 已改成量这个指标（原来拿参考音频当基准，差 8 dB，「配乐比人声响 4 dB」的片子也能判过）。临时救急：`BGM_ABS_DB=-18` 强制绝对增益，或 `BGM_DUCK=1`（默认开） |
| 改了 `substyle.py` 的字号/描边，**出片一点变化都没有** | `ip.json` 的 `subtitle.fs_cue` / `outline` **优先级高于引擎默认值**（`load_ip_overrides` → `apply_ip_overrides`）。只改引擎，出片时被 ip.json 的旧值原样顶回去。2026-10-27 用户提过两次「字号大一号、描边小一号」都没生效，就是这个原因 | **改字号必须双改**：`templates/scripts/substyle.py` 的 `FS_CUE`/`OUT_CUE` **和** `ip.json` 的 `fs_cue`/`outline`。改完跑 `python3 bin/vstudio.py algo <IP> <套次>` 看「正文字号」，再跑一次 `zone` 步骤验安全区 |

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
