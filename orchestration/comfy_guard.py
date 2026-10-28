#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyUI 显存 / 内存护栏 —— 「宁可卡慢，不要被杀；满了自动腾，腾不出就不开跑」。

子命令
======
  status                       看现状（内存/swap/显存/进程/近期内核 OOM）
  free                         强制卸载模型并**等释放真的生效**
  preflight                    提交任务前预检：不够就自动 free 再复检；仍不够 exit 1
  watch                        常驻守护：低于水位自动 free（批量出片时跑着）
  oom                          只看内核 OOM 记录

为什么必须有这道护栏（机制，不是玄学）
======================================
1. **「进程凭空消失 + 日志断在半句 + 没有 Traceback」= RAM 被内核 oom-killer
   SIGKILL，不是显存不足。**
   区分方法很硬：
     · 显存不足 → PyTorch 抛 `torch.OutOfMemoryError`，**必然留 Traceback**
     · RAM 不足 → 内核直接 SIGKILL，Python 侧**一个字节都写不出来**
   所以「没有 Traceback」恰恰是 RAM OOM 的**特征**，不是「原因不明」。
   排查要上抬一层看内核：`journalctl -k --since "-30min" | grep -i "out of memory"`，
   只看应用层日志永远查不到。

2. **POST /free 是异步的。** 发完请求立刻读 /proc/meminfo，读到的是卸载**之前**
   的数（会出现「刚释放完报 46G、下一行检查又变 26G」这种假象）。
   必须轮询到「可用内存连续两次不再增长」才算真的释放完。

3. **判据用 MemAvailable，不是 used。** used 把可回收的 page cache 算成占用，
   会让人误判为「快满了」。内核回收不了的只有匿名页（anon），
   MemAvailable 才是「现在还能申请到多少」。

4. **预检失败要拒绝开跑，而不是硬上。** 生成类任务一跑就是十几分钟 GPU 时间，
   跑到一半被内核杀掉，损失远大于「现在不开工」。

5. **长时间运行的进程内存是累积的。** 同一个工作流「昨天能跑、今天崩」是常态，
   不是代码退化。watch 守护 + 定期重启是正常运维动作。

配置（环境变量，均可用同名 CLI 参数覆盖）
========================================
  COMFY_URL            ComfyUI 地址，默认 http://127.0.0.1:8188
  GUARD_MIN_RAM_GB     可接受的最低可用内存，默认 12
  GUARD_MIN_VRAM_GB    可接受的最低可用显存，默认 6
  GUARD_FREE_WAIT      等释放生效的最长秒数，默认 45
  GUARD_WATCH_INTERVAL watch 轮询间隔秒，默认 30
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
DEF_MIN_RAM = float(os.environ.get("GUARD_MIN_RAM_GB", "12"))
DEF_MIN_VRAM = float(os.environ.get("GUARD_MIN_VRAM_GB", "6"))
DEF_WAIT = float(os.environ.get("GUARD_FREE_WAIT", "45"))
DEF_INTERVAL = float(os.environ.get("GUARD_WATCH_INTERVAL", "30"))

G = 1024.0 * 1024.0  # kB -> GB


# ---------------------------------------------------------------- 资源读数
def mem_info():
    """返回 dict(total, avail, used, swap_total, swap_free, swap_used) 单位 GB。"""
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            try:
                d[k.strip()] = int(v.split()[0])
            except (ValueError, IndexError):
                pass
    total = d.get("MemTotal", 0) / G
    avail = d.get("MemAvailable", 0) / G
    st = d.get("SwapTotal", 0) / G
    sf = d.get("SwapFree", 0) / G
    return {"total": total, "avail": avail, "used": total - avail,
            "swap_total": st, "swap_free": sf, "swap_used": st - sf}


def vram_info():
    """(free_gb, total_gb) 或 (None, None) —— 连不上说明 ComfyUI 可能已崩。"""
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=8) as r:
            d = json.loads(r.read().decode())
        dev = (d.get("devices") or [{}])[0]
        return dev.get("vram_free", 0) / 2**30, dev.get("vram_total", 0) / 2**30
    except Exception:
        return None, None


def comfy_alive() -> bool:
    return vram_info()[0] is not None


# ---------------------------------------------------------------- 释放
def do_free(wait_max: float = DEF_WAIT) -> float | None:
    """POST /free 并**轮询等到释放真的生效**再返回。

    ★ 异步陷阱见模块 docstring 第 2 条。这里用「可用内存连续 2 次增长 < 0.5G」
      作为稳定判据，避免读到一个中间态就返回。
    """
    req = urllib.request.Request(
        COMFY + "/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except urllib.error.URLError as e:
        print(f"[free] 请求失败: {e}", file=sys.stderr)
        return None

    t0, prev, stable = time.time(), None, 0
    while time.time() - t0 < wait_max:
        time.sleep(2)
        avail = mem_info()["avail"]
        if prev is not None and avail - prev < 0.5:
            stable += 1
            if stable >= 2:
                return avail
        else:
            stable = 0
        prev = avail
    return prev


# ---------------------------------------------------------------- 报告
def report(tag="[guard]"):
    m = mem_info()
    vf, vt = vram_info()
    print(f"{tag} 内存 已用 {m['used']:.1f}G / 总 {m['total']:.1f}G  |  "
          f"可用 {m['avail']:.1f}G  |  swap 用 {m['swap_used']:.1f}G / {m['swap_total']:.1f}G")
    if vf is None:
        print(f"{tag} ⚠ 连不上 ComfyUI（{COMFY}）—— 进程可能已被内核杀掉，需重启")
    else:
        print(f"{tag} 显存 空闲 {vf:.1f}G / 总 {vt:.1f}G")
    if m["swap_total"] > 0 and m["swap_free"] < 2:
        print(f"{tag} ⚠ swap 只剩 {m['swap_free']:.1f}G —— 匿名页无处可换，OOM 就在眼前")
    return m, vf


def oom_log(since="-7d"):
    try:
        p = subprocess.run(["journalctl", "-k", "--since", since, "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        hits = [l for l in p.stdout.splitlines() if "Out of memory: Killed" in l]
    except Exception:
        hits = []
    if hits:
        print(f"[oom] 近期被内核杀 {len(hits)} 次：")
        for l in hits[-5:]:
            print("   ", l.strip()[:120])
        print("[oom] ↑ 这是 RAM 不足的证据（显存不足会留 torch.OutOfMemoryError Traceback）")
    else:
        print("[oom] 无内核 OOM 记录")
    return len(hits)


# ---------------------------------------------------------------- 预检
def preflight(min_ram=DEF_MIN_RAM, min_vram=DEF_MIN_VRAM,
              auto_free=True, wait=DEF_WAIT) -> bool:
    """提交前必跑：不够就自动释放再复检；仍不够返回 False（调用方应拒绝开跑）。"""
    if not comfy_alive():
        print("[preflight] ✗ ComfyUI 不可达，先把它拉起来", file=sys.stderr)
        return False

    m, vf = mem_info(), vram_info()[0]
    need_free = (m["avail"] < min_ram) or (vf is not None and vf < min_vram)
    if need_free and auto_free:
        print(f"[preflight] 可用内存 {m['avail']:.1f}G / 显存 "
              f"{'?' if vf is None else f'{vf:.1f}G'} 低于水位，先释放…")
        do_free(wait)
        m, vf = mem_info(), vram_info()[0]
        print(f"[preflight] 释放后：内存 {m['avail']:.1f}G / 显存 "
              f"{'?' if vf is None else f'{vf:.1f}G'}")

    bad = []
    if m["avail"] < min_ram:
        bad.append(f"可用内存 {m['avail']:.1f}G < 要求 {min_ram:.0f}G")
    if vf is not None and vf < min_vram:
        bad.append(f"可用显存 {vf:.1f}G < 要求 {min_vram:.0f}G")
    if bad:
        print("[preflight] ✗ " + "；".join(bad), file=sys.stderr)
        print("   ⚠ 硬跑会被内核 oom-killer 杀掉且不留 Traceback，白烧 GPU 时间",
              file=sys.stderr)
        print("   对策：重启 ComfyUI 释放累积内存，或调低分辨率/帧数再跑",
              file=sys.stderr)
        return False

    print(f"[preflight] ✓ 资源充足（内存 {m['avail']:.1f}G，显存 "
          f"{'?' if vf is None else f'{vf:.1f}G'}）")
    return True


# ---------------------------------------------------------------- 守护
def watch(min_ram=DEF_MIN_RAM, min_vram=DEF_MIN_VRAM,
          interval=DEF_INTERVAL, max_rounds=0):
    """常驻守护：低于水位就自动释放。

    max_rounds=0 表示无限。连续 3 轮释放后仍不达标 → 提示必须重启，并退出。
    """
    print(f"[watch] 水位 RAM≥{min_ram:.0f}G VRAM≥{min_vram:.0f}G，"
          f"每 {interval:.0f}s 检查一次。Ctrl-C 停止。")
    rounds, strikes = 0, 0
    try:
        while max_rounds == 0 or rounds < max_rounds:
            rounds += 1
            if not comfy_alive():
                print("[watch] ⚠ ComfyUI 不可达（多半已被 OOM 杀掉），跳过本轮")
                time.sleep(interval)
                continue
            m = mem_info()
            vf = vram_info()[0]
            if (m["avail"] < min_ram) or (vf is not None and vf < min_vram):
                print(f"[watch] 低于水位（RAM {m['avail']:.1f}G / VRAM "
                      f"{'?' if vf is None else f'{vf:.1f}G'}）→ 释放")
                do_free()
                m2, vf2 = mem_info(), vram_info()[0]
                still_bad = (m2["avail"] < min_ram) or (vf2 is not None and vf2 < min_vram)
                strikes = strikes + 1 if still_bad else 0
                if strikes >= 3:
                    print("[watch] ✗ 连续 3 轮释放后仍不达标 —— 内存已被长期累积占死，"
                          "**必须重启 ComfyUI**（这是设计内的运维动作，不是故障）",
                          file=sys.stderr)
                    return 1
            else:
                strikes = 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch] 已停止")
    return 0


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="ComfyUI 显存/内存护栏")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "free", "preflight", "watch", "oom"])
    ap.add_argument("--min-ram", type=float, default=DEF_MIN_RAM)
    ap.add_argument("--min-vram", type=float, default=DEF_MIN_VRAM)
    ap.add_argument("--wait", type=float, default=DEF_WAIT)
    ap.add_argument("--interval", type=float, default=DEF_INTERVAL)
    ap.add_argument("--rounds", type=int, default=0, help="watch 轮数，0=无限")
    ap.add_argument("--no-auto-free", action="store_true", help="preflight 只检查不释放")
    a = ap.parse_args()

    if a.cmd == "status":
        report()
    elif a.cmd == "oom":
        oom_log()
    elif a.cmd == "free":
        before = mem_info()["avail"]
        after = do_free(a.wait)
        print(f"[free] 可用内存 {before:.1f}G → "
              f"{'?' if after is None else f'{after:.1f}G'}")
        report()
    elif a.cmd == "preflight":
        sys.exit(0 if preflight(a.min_ram, a.min_vram,
                                not a.no_auto_free, a.wait) else 1)
    elif a.cmd == "watch":
        sys.exit(watch(a.min_ram, a.min_vram, a.interval, a.rounds))


if __name__ == "__main__":
    main()
