#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyUI 显存/内存释放 + 内存安全预检。

用法:
  python3 comfy_free.py            # 释放 + 打印内存/显存现状
  python3 comfy_free.py --check    # 只检查，不释放（出片前必跑）
  python3 comfy_free.py --need 30  # 要求至少 30GB 可用 RAM，不足则 exit 1

★★ 2026-10-27 血泪教训（S04 第一版被内核杀）
=============================================
现象：ComfyUI 进程**凭空消失**，日志停在「VAE prepared for dynamic VRAM
loading. 4965MB Staged.」之后，**没有任何 Traceback**。

我当时把这个信号读成了「显存耗尽」—— **完全错了**。
正确读法：
  · 显存不足 → PyTorch 抛 `torch.OutOfMemoryError`，**必然留 Traceback**
  · RAM 不足 → 内核 oom-killer 直接 SIGKILL，**Python 侧一个字都留不下**

⇒ **「日志突然断 + 无 Traceback + 进程消失」= RAM 被内核回收，不是显存。**

内核证据（journalctl -k）：
    Out of memory: Killed process 1480009 (python)
      anon-rss:50206504kB      ← 47.9 GB，占 61.5GB 总内存的 78%
    OOM 瞬间 Node 0 Normal free: 125380kB  ← 只剩 122 MB

为什么 47.9GB —— `run.sh` 的两个参数在「用内存换速度」：
  `--high-ram`     官方说明：「Can improve performance slightly on high RAM or on
                   systems where **pagefile use is preferred over model loading**」
                   → 宁可把模型留在 RAM，也不卸载重载
  `--async-offload 2`  两个异步卸载流，每流在 RAM 里各留一份权重副本

于是同一个 prompt 内部，TextEncoder（**14956MB**）被反复 stage 了 7 次、
MiniMaxH3（19995MB）2 次，全程 `0 models unloaded`：
    14956×3 + 19995 + 4965(VAE) + 576(音频VAE) + 674(LatentResizer) ≈ 71GB 申请量
实际峰值 47.9GB（部分被 free/复用吃掉），仍把自己顶死了。

⚠️ 关键认知：**这不是 S04 特有的**。14:06–14:18 就已经出现过同样的重复
stage，只是当时没到临界点。**同一个进程跑满几小时后，内存是累积上去的**，
所以「昨天能跑、今天崩溃」是正常的 —— 不是代码退化。

⇒ 因此本脚本现在同时看**内存**，不只是显存。
   `gen_video.py` 在提交前会调 `--check`，不足就拒绝开跑，
   而不是等 20 分钟后被内核杀掉（那时已经烧了 20 分钟的 GPU 时间）。
"""
import json
import os
import sys
import time
import urllib.request

COMFY = "http://127.0.0.1:8188"


def mem_info():
    """读 /proc/meminfo，返回 (总GB, 可用GB, 已用GB, swap已用GB)。"""
    d = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                d[k.strip()] = int(v.split()[0])     # kB
    except Exception:
        return None
    g = lambda kb: kb / 1024 / 1024
    total = g(d.get("MemTotal", 0))
    avail = g(d.get("MemAvailable", 0))
    swap_used = g(d.get("SwapTotal", 0) - d.get("SwapFree", 0))
    return total, avail, total - avail, swap_used


def vram_info():
    """读 ComfyUI system_stats，返回 (free GB, total GB) 或 (None, None)。"""
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=10) as r:
            d = json.loads(r.read().decode())
        dev = d.get("devices", [{}])[0]
        return dev.get("vram_free", 0) / 2**30, dev.get("vram_total", 0) / 2**30
    except Exception:
        return None, None


def do_free(wait_max=45):
    """请求卸载全部模型，并**等它真的生效**再返回。

    ★ 2026-10-27 补：原来发完 POST 就立刻返回、紧接着读 /proc/meminfo，
      读到的是卸载**之前**的数 —— 于是 `comfy_free.py` 报「可用 46G」，
      而下一行 `--check` 又说「可用 26G」，看起来像释放失败。

      实际是 Free 请求是异步的：ComfyUI 把模型从 RAM 里丢掉需要几秒
      （H3 全套 42GB，还要等 CUDA 分配器把缓存交还）。
      这里改成轮询到「可用内存连续两次不再增长」或超时，再返回。
    """
    req = urllib.request.Request(COMFY + "/free",
                                 data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()

    # 轮询等释放生效：可用内存不再明显增长（<0.5G 波动）就认为稳定了
    t0 = time.time()
    prev = None
    stable = 0
    while time.time() - t0 < wait_max:
        time.sleep(2)
        m = mem_info()
        if not m:
            break
        avail = m[1]
        if prev is not None and avail - prev < 0.5:
            stable += 1
            if stable >= 2:          # 连续两次几乎不涨 → 稳定
                return avail
        else:
            stable = 0
        prev = avail
    return prev


def report(tag="[free]"):
    m = mem_info()
    vf, vt = vram_info()
    if m:
        total, avail, used, swap = m
        print(f"{tag} 内存 {used:.1f}G 已用 / {total:.1f}G 总  |  可用 {avail:.1f}G"
              f"  |  swap {swap:.1f}G")
    if vf is not None:
        print(f"{tag} 显存 {vf:.1f}G 空闲 / {vt:.1f}G 总")
    else:
        print(f"{tag} ⚠ 连不上 ComfyUI（{COMFY}）—— 可能已崩溃，需重启 run.sh")
    return m, vf


def main():
    args = sys.argv[1:]
    check_only = "--check" in args
    need = None
    if "--need" in args:
        i = args.index("--need")
        try:
            need = float(args[i + 1])
        except (IndexError, ValueError):
            sys.exit("✗ --need 后面要跟一个数字（GB）")

    if not check_only:
        try:
            before = mem_info()
            b = f"{before[1]:.1f}G" if before else "?"
            avail_after = do_free()
            a = f"{avail_after:.1f}G" if avail_after else "?"
            print(f"[free] 已卸载全部模型：可用内存 {b} → {a}")
        except Exception as e:
            print("[free] 请求失败:", e)
            sys.exit(1)

    m, vf = report()

    if need is not None:
        if not m:
            sys.exit("✗ 读不到 /proc/meminfo，无法判断内存是否够")
        avail = m[1]
        if avail < need:
            sys.exit(f"✗ 可用内存 {avail:.1f}G < 要求 {need:.0f}G\n"
                     f"   ⚠ 直接开跑会被内核 oom-killer 杀掉，且**不留 Traceback**\n"
                     f"   （2026-10-27 S04 就这样丢掉 20 分钟 GPU 时间）\n"
                     f"   对策：重启 ComfyUI（cd /cfu/ComfyUI && ./run.sh）后再跑")
        print(f"[free] ✓ 内存充足（可用 {avail:.1f}G ≥ 要求 {need:.0f}G）")


if __name__ == "__main__":
    main()
