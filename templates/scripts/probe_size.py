#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一的分辨率探测：读写视频/图片的真实宽高，不再写死 768x1376。

为什么需要它
============
2026-10-27 起出片支持多档分辨率（MP=1.0 → 768x1376，MP=1.33 → 896x1568，
MP=2.0 → 1088x1920）。原先所有验收工具都写死 `W, H = 768, 1376`，
换个档位后 `face_box` / `top_of` / `safe_zone` 的坐标全错，
量出来的 p99、过曝率、安全区余量**全是假数据而且不会报错** ——
这是最危险的一类 bug：数字看着正常，结论完全错。

用法（作为模块）
==============
    from probe_size import probe, W_H_DEFAULT
    W, H = probe(video_path)          # 自动读

用法（命令行，方便脚本里塞）
==========================
    SIZE=$(python3 probe_size.py video.mp4)   # 输出 "896 1568"
"""
import re
import subprocess
import sys

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

W_H_DEFAULT = (768, 1376)          # S01/S02/S03 定版尺寸，探测失败时的兜底


def probe(path):
    """返回 (W, H)。视频取 video stream，图片取实际像素尺寸。失败返回兜底值。"""
    try:
        p = subprocess.run([FF, "-nostdin", "-hide_banner", "-i", path], capture_output=True)
        log = p.stderr.decode(errors="replace")
    except Exception:
        return W_H_DEFAULT

    # 优先匹配 Video 行里的 `768x1376`
    for line in log.splitlines():
        if "Video:" in line:
            m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                if w > 1 and h > 1:
                    return w, h
    # 图片没有 Video 行，用 "Stream ... 1088x1920" 兜底
    m = re.search(r"Stream #\S+.*?(\d{2,5})x(\d{2,5})", log)
    if m:
        return int(m.group(1)), int(m.group(2))
    return W_H_DEFAULT


def is_default(size):
    return tuple(size) == W_H_DEFAULT


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"{W_H_DEFAULT[0]} {W_H_DEFAULT[1]}")
        sys.exit(0)
    w, h = probe(sys.argv[1])
    print(f"{w} {h}")
