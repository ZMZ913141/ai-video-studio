#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片收尾：曝光校正。

v6（旧）vs v7（当前）
--------------------
v6 曲线肩部从 0.90 才开始（0.90→0.885），只能压掉最顶上一点。
实测 S03 后段的 >230% 从首帧 0.137% 涨到末帧 0.875%（6.4 倍），
爆的是 **215–245 这一整段**，肩部开在 0.90 根本够不着。

v7 把肩部前移到 0.70（0.70 起每个节点依次下压 0.695/0.830/0.885/0.905），
gamma 保持 1.12 把中间调撑住。实测同 4 个时间点（0.5/20/30/37s）：

  方案            末帧>230%   平Δ脸均   平Δ全帧p85
  v6 原样          0.579      +11.0      +6.2
  v7（肩0.70）     0.000      +11.1      +2.2   ← 选它
  B（肩0.65）      0.000      +10.2      -0.8
  C（肩0.60）      0.000       +9.1      -4.8

v7 相比 v6：过曝面积归零，脸均一分不差（不闷），墙亮度只飘 +2.2，
纯收益。B/C 会开始把整体调暗，不要。

对比记录（v6_full 原片 / g1.12 / g1.12+hi / g1.08+hi）：
  帧 p85      186.2 / 195.6 / 194.4 / 193.4
  脸均        116.9 / 127.4 / 127.5 / 125.6
  >230% 均值   0.83 /  1.60 /  0.77 /  0.76

用法: finish_v6.py <in.mp4> <out.mp4>
"""
import os
import subprocess
import sys

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

# ffmpeg eq 的 gamma>1 才是提亮（gamma<1 更暗，别搞反）
# v7：肩部 0.70 起，把 215-245 那段过曝压掉
CURVE_V7 = "0/0 0.60/0.60 0.70/0.695 0.86/0.830 0.96/0.885 1/0.905"
# v6：旧参数，留作回退（肩 0.90，压不住后段）
CURVE_V6 = "0/0 0.70/0.70 0.90/0.885 1/0.95"

CURVE = os.environ.get("CURVE", CURVE_V7)
GRADE = (f"eq=gamma=1.12:brightness=0.000:contrast=1.01:saturation=1.02,"
         f"curves=all='{CURVE}'")


def main():
    src, dst = sys.argv[1], sys.argv[2]
    fc = f"[0:v]{GRADE},format=yuv420p[v]"
    cmd = [FF, "-nostdin", "-y", "-i", src, "-filter_complex", fc,
           "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "slow", "-crf", "16",
           "-pix_fmt", "yuv420p", "-r", "24", "-threads", "2",
           "-c:a", "copy", "-movflags", "+faststart", dst]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        print(p.stderr.decode()[-1500:])
        sys.exit(1)
    print("OK", dst, os.path.getsize(dst), flush=True)


if __name__ == "__main__":
    main()
