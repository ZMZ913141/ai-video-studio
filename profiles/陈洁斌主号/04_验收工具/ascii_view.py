#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把画面降采样成 ASCII 灰度图，用于人工判读构图（本环境看不了图片）。

用法: ascii_view.py <file> <时间秒> [列数]
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
RAMP = " .:-=+*#%@"


def main():
    path, t = sys.argv[1], float(sys.argv[2])
    cw = int(sys.argv[3]) if len(sys.argv) > 3 else 56
    ch = max(8, int(cw * 1376 / 768 / 2.1))
    p = subprocess.run([FF, "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                        "-vf", f"scale={cw}:{ch}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    d = np.frombuffer(p.stdout, np.uint8)[:cw * ch].reshape(ch, cw).astype(np.float32)
    print(f"=== {path} @ {t}s  ({cw}x{ch}) 均值 {d.mean():.1f} ===")
    lo, hi = float(d.min()), float(d.max())
    print(f"    亮度范围 {lo:.0f}..{hi:.0f}")
    for y in range(ch):
        line = "".join(RAMP[min(len(RAMP) - 1, int((v - lo) / max(hi - lo, 1) * len(RAMP)))]
                       for v in d[y])
        print(f"{int(y * 1376 / ch):4d}|{line}|")


if __name__ == "__main__":
    main()
