#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""景别量化 v2：用"暗像素横向宽度"定位头顶。

v1 用中列第一个暗段 → 会被画面上方暗角/暗边骗到（top_frac 错误地报 0.03）。
真实头顶的特征是：一行里黑发占据很宽的一段（>120px），而暗边/暗角只是贴边的窄条。
所以：逐行统计 <85 的像素数，取第一个"宽度达标且连续 >=12 行"的行段起点作为头顶。

用法:
  topcheck.py <file> [时间秒 ...]
  不传时间则取若干采样点。
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
W, H = 768, 1376
DARK = 85
MIN_W_BASE = 165  # 768 宽下的发宽门槛：真实发宽 182-210，顶部暗带只有 140-160
MIN_W = MIN_W_BASE
MIN_ROWS = 12
Y0 = 0.10          # 从 10% 高度开始找，避开顶部暗角/暗带


def _probe_wh(path):
    import os
    _p = os.path.dirname(os.path.abspath(__file__))
    if _p not in sys.path:
        sys.path.insert(0, _p)
    try:
        from probe_size import probe
        return probe(path)
    except Exception:
        return W, H


def set_size(w, h):
    """按分辨率缩放像素级门槛。

    ⚠️ MIN_W 是**绝对像素**门槛，1.33MP（896 宽）下发宽会等比放大到 ~213px，
    沿用 165 会让顶部暗带（150 左右）也通过，top 被钉在扫描下界 → 假数据。
    必须按 W/768 等比放大。
    """
    global W, H, MIN_W, MIN_ROWS
    W, H = w, h
    MIN_W = int(round(MIN_W_BASE * w / 768))
    MIN_ROWS = max(8, int(round(12 * h / 1376)))
    return W, H


def gray(path, t=0.0):
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                        "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    if len(p.stdout) < W * H:
        return None
    return np.frombuffer(p.stdout, np.uint8)[:W * H].reshape(H, W).astype(np.float32)


def top_row(d):
    widths = (d < DARK).sum(axis=1)
    ok = widths >= MIN_W
    run, start = 0, None
    for y in range(int(H * Y0), H):
        if ok[y]:
            if start is None:
                start = y
            run += 1
            if run >= MIN_ROWS:
                return start, int(widths[start:start + MIN_ROWS].mean())
        else:
            start, run = None, 0
    return None, 0


def stats(path, t):
    d = gray(path, t)
    if d is None:
        return None
    y, w = top_row(d)
    if y is None:
        return None
    # 头顶上方墙体亮度（判断曝光是否稳定）
    wall = float(d[max(0, y - 120):max(1, y - 40), W // 3: 2 * W // 3].mean())
    return y / H, w, wall, float(d.mean())


def main():
    path = sys.argv[1]
    _w, _h = _probe_wh(path)
    set_size(_w, _h)                       # ★ 门槛随分辨率等比缩放
    times = [float(x) for x in sys.argv[2:]] or [0.5, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    print(f"=== {path} ===  {W}x{H}  MIN_W={MIN_W}")
    print("  时间   top_frac  发宽  墙亮  整帧均值")
    for t in times:
        r = stats(path, t)
        if r is None:
            print(f"  {t:5.2f}   定位失败")
        else:
            print(f"  {t:5.2f}   {r[0]:.4f}   {r[1]:4d}  {r[2]:5.1f}  {r[3]:5.1f}")


if __name__ == "__main__":
    main()
