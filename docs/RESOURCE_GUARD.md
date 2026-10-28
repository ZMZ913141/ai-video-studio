# ComfyUI 资源护栏（显存 / 内存）

单卡 4080 SUPER 16G 跑 WAN / FLUX 这类模型，崩只有两种形态。分清是哪一种，
决定了你怎么处理——**判断错了会往完全相反的方向排查**。

## 一、崩溃形态判读（最重要的一节）

| 症状 | 真实原因 | 证据在哪 |
|---|---|---|
| `torch.OutOfMemoryError` + **有 Traceback** | **显存（VRAM）不足** | Python 日志 |
| 日志戛然而止 + **无 Traceback** + 进程消失 | **RAM 不足，被内核 oom-killer SIGKILL** | `journalctl -k` |

关键点：**「没有 Traceback」不是"原因不明"，它是 RAM OOM 的特征。**
内核杀进程时，被杀的 Python 一个字节都写不出来，所以必然不留 Traceback。

第一件事永远是查内核，而不是猜哪个显存分配失败：

```bash
journalctl -k --since "-30min" | grep -i "out of memory"
# 典型输出：Killed process <pid> (python) anon-rss:<kB>
```

⚠️ 只看 `journalctl --user _PID=<pid>`（应用层）永远查不到——必须上抬一层到内核。

## 二、四条硬规则

1. **判据用 `MemAvailable`，不是 `used`。**
   `used` 把可回收的 page cache 算成占用，会误判成"快满了"。
   内核回收不了的只有匿名页（anon），`MemAvailable` 才是"现在还能申请到多少"。

2. **`POST /free` 是异步的。** 发完请求立刻读内存，读到的是卸载**之前**的数
   （会出现"刚释放完报 46G、下一行检查又变 26G"的假象，看着像释放失败）。
   必须轮询到「可用内存连续两次增长 < 0.5G」才算真的释放完。
   `comfy_guard.py` 已实现，别自己裸调 `/free` 就往下走。

3. **预检失败要拒绝开跑，而不是硬上。**
   生成任务一跑十几分钟 GPU 时间，跑到一半被杀损失远大于"现在不开工"。
   `preflight` 返回 False 时，pipeline-service 直接 503、MCP 直接抛错。

4. **长时间运行的进程内存是累积的。**
   同一个工作流"昨天能跑、今天崩"是常态，**不是代码退化**。
   批量出片时开着 `watch` 守护；连续 3 轮释放仍不达标就必须重启 ComfyUI——
   这是设计内的运维动作。

## 三、用法

```bash
cd /home/qmr/github/ai-video-studio/orchestration

python3 comfy_guard.py status              # 看现状
python3 comfy_guard.py free                # 强制释放（等生效）
python3 comfy_guard.py preflight           # 出片前必跑；不够会先自动释放再复检
python3 comfy_guard.py preflight --min-ram 30 --min-vram 12   # 大模型任务抬高水位
python3 comfy_guard.py oom                 # 查近期内核 OOM 记录
python3 comfy_guard.py watch               # 常驻守护，低于水位自动释放（批量出片时开着）
```

`preflight` 退出码：`0` 可开工 / `1` 拒绝（调用方应中止）。

## 四、自动接入（已内置，无需手工调用）

- **MCP server**（`mcp-servers/om-comfyui`）：`comfyui_generate` 提交前自动 `preflight`，
  任务结束（成功或失败）自动按需 `release`；另暴露 `comfyui_status` / `comfyui_free` 两工具。
- **pipeline-service**：`/generate` 同样内置；另加 REST 端点
  `GET /guard/status`、`POST /guard/free`、`POST /guard/preflight`。

**"按需释放"的含义**：任务结束后只有余量低于水位 1.5 倍时才真释放，
连续作业时避免模型反复加载/卸载（每次重载都要几十秒到几分钟）。

## 五、水位配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GUARD_MIN_RAM_GB` | 12 | 可接受的最低可用内存 |
| `GUARD_MIN_VRAM_GB` | 6 | 可接受的最低可用显存 |
| `GUARD_FREE_WAIT` | 45 | 等释放生效的最长秒数 |
| `GUARD_WATCH_INTERVAL` | 30 | watch 轮询间隔 |
| `COMFY_AUTO_GUARD` | 1 | 提交前预检开关 |
| `COMFY_AUTO_RELEASE` | 1 | 任务后按需释放开关 |

跑 WAN 14B 这类大模型时把 `GUARD_MIN_RAM_GB` 抬到 25~30。

## 六、Swap 是最后一道防线

Linux 的 swap 是**固定大小**，写满就没了，内核只能杀人
（Windows 的 pagefile 会自动增长，所以体感不一样）。
匿名页不可回收，只能靠 swap——**swap 一满就必然 OOM**。
`status` 里 swap 剩余 < 2G 会告警。

扩容（需 root，Agent 侧提不了权，自己执行）：
```bash
sudo fallocate -l 32G /swapfile.extra && sudo chmod 600 /swapfile.extra
sudo mkswap /swapfile.extra && sudo swapon -p 10 /swapfile.extra
echo '/swapfile.extra none swap sw,pri=10 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=80' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl -w vm.swappiness=80
```
