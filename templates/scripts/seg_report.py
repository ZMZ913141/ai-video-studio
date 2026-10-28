#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把音频按停顿切成语音段，报告每段时长与语速，定位哪里慢/异常。

用法: seg_report.py <audio> [字数]
"""
import re
import subprocess
import sys

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")


def probe(path):
    p = subprocess.run([FF, "-i", path], capture_output=True)
    dur = 0.0
    for l in p.stderr.decode().splitlines():
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", l)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur


def silences(path, noise=-32, d=0.15):
    p = subprocess.run([FF, "-i", path, "-af", f"silencedetect=noise={noise}dB:d={d}",
                        "-f", "null", "-"], capture_output=True)
    out = []
    s = None
    for l in p.stderr.decode().splitlines():
        m = re.search(r"silence_start: ([\d.]+)", l)
        if m:
            s = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", l)
        if m and s is not None:
            out.append((s, float(m.group(1))))
            s = None
    return out


def main():
    path = sys.argv[1]
    total = float(sys.argv[2]) if len(sys.argv) > 2 else probe(path)
    sil = silences(path)
    print(f"总时长 {total:.2f}s   停顿 {len(sil)} 处")

    # 语音区间 = 停顿之间的部分
    segs, cur = [], 0.0
    for a, b in sil:
        if a - cur > 0.05:
            segs.append((cur, a))
        cur = b
    if total - cur > 0.05:
        segs.append((cur, total))

    print(f"\n{'#':>3} {'起':>7} {'止':>7} {'时长':>6}  {'停顿后':>7}")
    speech = 0.0
    for i, (a, b) in enumerate(segs):
        gap = ""
        for sa, sb in sil:
            if abs(sb - a) < 0.01:
                gap = f"{sb-sa:.2f}s"
        speech += b - a
        print(f"{i+1:3d} {a:7.2f} {b:7.2f} {b-a:6.2f}  {gap:>7}")
    print(f"\n纯语音 {speech:.2f}s   静音 {total-speech:.2f}s   "
          f"静音占比 {100*(total-speech)/total:.1f}%")


if __name__ == "__main__":
    main()
