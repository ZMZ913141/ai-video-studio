#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐帧量化接缝：帧间差 + 头顶位置，判断接线处是否跳。

用法: seam_check.py <video> [接缝输出帧号 ...]
不传帧号则全片扫描，只报告异常峰。
"""
import os
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
W, H = 768, 1376
SW, SH = 96, 172          # 分析用小图，够判景别和曝光，且快


def frames(path):
    """以 rawvideo 流式读全片小图，避免一次吃满内存。"""
    p = subprocess.Popen(
        [FF, "-v", "error", "-i", path, "-vf", f"scale={SW}:{SH}",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE)
    per = SW * SH
    while True:
        buf = p.stdout.read(per)
        if len(buf) < per:
            break
        yield np.frombuffer(buf, np.uint8).reshape(SH, SW).astype(np.float32)
    p.stdout.close()
    p.wait()


def top_frac(g):
    """沿中列找第一段连续暗像素（黑发），返回头顶相对高度。"""
    col = g[:, SW // 2 - 6: SW // 2 + 6].mean(axis=1)
    dark = col < 85
    run = 0
    for y in range(SH):
        if dark[y]:
            run += 1
            if run >= 3:
                return (y - 2) / SH
        else:
            run = 0
    return float("nan")


def main():
    path = sys.argv[1]
    seams = [int(x) for x in sys.argv[2:]]
    prev = None
    diffs, tops = [], []
    for g in frames(path):
        diffs.append(0.0 if prev is None else float(np.abs(g - prev).mean()))
        tops.append(top_frac(g))
        prev = g
    diffs = np.array(diffs)
    tops = np.array(tops)
    n = len(diffs)
    print(f"帧数 {n}")

    body = diffs[3:]
    med, mad = float(np.median(body)), float(np.median(np.abs(body - np.median(body))))
    print(f"帧间差 中位 {med:.2f}  MAD {mad:.2f}  最大 {body.max():.2f} @ {int(body.argmax())+3}")

    if not seams:
        thr = med + 8 * max(mad, 0.5)
        peaks = [(i, diffs[i]) for i in range(3, n) if diffs[i] > thr]
        peaks.sort(key=lambda x: -x[1])
        print(f"异常峰阈值 {thr:.2f}：{len(peaks)} 个")
        for i, d in peaks[:8]:
            print(f"  帧 {i} ({i/24:.3f}s) 差 {d:.2f}")
        return

    for f in seams:
        lo, hi = max(1, f - 4), min(n, f + 4)
        print(f"\n接缝 {f} ({f/24:.3f}s):")
        for i in range(lo, hi):
            mark = "  <== 接缝" if i == f else ""
            print(f"  帧 {i:4d} 差 {diffs[i]:6.2f}  top {tops[i]:.4f}{mark}")
        # 接缝处是否显著高于邻域
        near = [diffs[i] for i in range(lo, hi) if i != f]
        print(f"  接缝差 {diffs[f]:.2f} / 邻域中位 {np.median(near):.2f} "
              f"→ 倍数 {diffs[f]/max(np.median(near),1e-6):.2f}")
        dt = abs(tops[f] - tops[f-1]) if f < n and not np.isnan(tops[f]) else float('nan')
        print(f"  top_frac 跳变 {dt:.4f}")


if __name__ == "__main__":
    main()
