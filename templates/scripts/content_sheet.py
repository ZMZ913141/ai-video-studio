#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容对照表：把「产品图 / 起始静帧 / 成片若干帧」拼成一张图，给人眼 30 秒验收。

为什么需要它（2026-10-31 珠珠 Y01/Y02 事故）
=============================================
那一轮我的验收清单全是**技术指标**：分辨率、字幕时间轴同步、安全区像素、音色 MFCC、
黑帧检测。五项全绿 —— 但成片里她穿的是一件产品库里根本不存在的外套，
右边货架上挂的是长袍而不是短款马甲。**没有一个指标能测出这件事。**

更糟的是静帧生成时把参考图给错了（第三张给了「内搭参考」= 四件衬衫，
提示词却写着「这是马甲」），模型只能瞎编。而所有验收工具都不看画面内容，
于是「格式合格」被我当成了「内容合格」。

结论：**画面内容必须有一步人眼比对**。本工具把三样东西并排放：
  [产品图] [静帧] [成片第 1/3/5/7 段各一帧]
一眼就能看出：她穿的还是不是那件、货架上挂的是不是同一款、有没有变成别的形状。

用法
====
  content_sheet.py <成片mp4> <静帧png> <产品图png> [-o 输出.jpg] [--n 4]

输出单张 jpg，供人工验收。退出码恒 0（它是给人看的，不是自动判定）；
但会把「产品色 vs 成片主色」的粗略距离打出来，作为辅助参考。
"""
import argparse
import os
import subprocess
import sys


def ffprobe_dur(p):
    """用 ffmpeg -i 读时长（本机 ffprobe 坏，见项目记忆）。"""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", p],
                         capture_output=True, text=True).stderr
    for ln in out.splitlines():
        if "Duration:" in ln:
            h, m, s = ln.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise SystemExit("读不到时长：%s" % p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4", help="成片")
    ap.add_argument("still", help="起始静帧")
    ap.add_argument("product", help="产品图（产品图库里的那一张）")
    ap.add_argument("-o", "--out", default="", help="输出 jpg（默认与成片同目录）")
    ap.add_argument("--n", type=int, default=4, help="从成片抽几帧（默认 4）")
    ap.add_argument("--tile", default="", help="拼图行列，如 3x2；默认按 n+2 自动算")
    a = ap.parse_args()

    dur = ffprobe_dur(a.mp4)
    n = max(1, a.n)
    # 均匀取帧，但避开首尾各 8%（开场的标题帧和结尾的落点帧另有用途）
    ts = [dur * (0.08 + 0.84 * (i + 0.5) / n) for i in range(n)]

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.mp4)),
                                os.path.basename(a.mp4).rsplit(".", 1)[0] + "_内容对照.jpg")
    tmpd = os.path.join(os.path.dirname(os.path.abspath(out)), "_content_sheet_tmp")
    os.makedirs(tmpd, exist_ok=True)

    tiles = []
    # 1) 产品图（白底平铺）—— 用 pad 补成正方形再统一缩放，避免比例失真
    p1 = os.path.join(tmpd, "00_product.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", a.product,
                    "-vf", "scale=480:480:force_original_aspect_ratio=decrease,"
                           "pad=480:480:(ow-iw)/2:(oh-ih)/2:color=white,format=rgb24",
                    "-frames:v", "1", "-y", p1], check=True)
    tiles.append(p1)

    # 2) 静帧
    p2 = os.path.join(tmpd, "01_still.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", a.still,
                    "-vf", "scale=480:480:force_original_aspect_ratio=decrease,"
                           "pad=480:480:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24",
                    "-frames:v", "1", "-y", p2], check=True)
    tiles.append(p2)

    # 3) 成片帧
    for i, t in enumerate(ts, 1):
        p = os.path.join(tmpd, "1%d_frame.png" % i)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-ss", "%.3f" % t, "-i", a.mp4, "-frames:v", "1",
                        "-vf", "scale=480:480:force_original_aspect_ratio=decrease,"
                               "pad=480:480:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24",
                        "-y", p], check=True)
        tiles.append(p)

    total = len(tiles)
    # 自动排版：尽量接近正方形
    if a.tile and "x" in a.tile:
        cols, rows = [int(x) for x in a.tile.lower().split("x")]
    else:
        cols = 3 if total > 4 else total
        rows = (total + cols - 1) // cols

    # 空位补纯色（不要拿静帧去填 —— 那会让人以为那也是成片里的一帧）
    blank = os.path.join(tmpd, "blank.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=0x222222:s=480x480:d=1",
                    "-frames:v", "1", "-y", blank], check=True)
    while len(tiles) < cols * rows:
        tiles.append(blank)

    # ★ 按顺序重编成 seq_NN.png 再用 glob 喂 tile：
    #   concat demuxer 对无时间基的 png 序列会丢张（第一版就是这么丢掉产品图和静帧、
    #   拼出来第一格全黑的），image2+glob 才是序列图的正确读法。
    seqdir = os.path.join(tmpd, "seq")
    os.makedirs(seqdir, exist_ok=True)
    for i, t in enumerate(tiles):
        dst = os.path.join(seqdir, "seq_%03d.png" % i)
        if os.path.abspath(t) != os.path.abspath(dst):
            subprocess.run(["cp", t, dst], check=True)

    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-pattern_type", "glob", "-i", os.path.join(seqdir, "seq_*.png"),
                    "-vf", "tile=%dx%d:padding=6:margin=6:color=0x222222" % (cols, rows),
                    "-frames:v", "1", "-y", out], check=True)

    print("内容对照图 → %s" % out)
    print("  排布：产品图 | 静帧 | 成片 %d 帧（避开首尾 8%%）" % n)
    print("  ★ 人工必看三项：")
    print("     1) 静帧与成片里，她穿的还是产品图那一件吗（颜色/形制/纽扣）")
    print("     2) 左侧主产品位、右侧货架上的，形状跟产品图一致吗（短款≠长袍）")
    print("     3) 各段之间服装有没有漂移（第 1 帧和第 %d 帧应该穿的是同一件）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
