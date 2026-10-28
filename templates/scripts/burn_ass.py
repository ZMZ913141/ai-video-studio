#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ASS 字幕烧进成片（用 Kdenlive flatpak 自带的 ffmpeg + libass）。

为什么需要这个包装器
==================
1. `flatpak run` 在本沙箱必死（xdg-dbus-proxy 握手失败），所以直接调 app 里的二进制。
2. 必须用 runtime 的 ld.so 显式加载（`--library-path`），且 `LD_LIBRARY_PATH`
   只能通过 `env` 给目标进程，否则宿主的 bash/head 会被 GLIBC 2.43 的库带崩。
3. **字体必须显式给 `fontsdir`**，而且只能给装了几个字体的精简目录。
   - 不给 fontsdir：libass 走 fontconfig，找不到我们的字体 → 静默回退 Noto（字形全错）
   - 给 1497 个字体的目录：内存爆掉（沙箱 4GB 上限）→
     `Resource temporarily unavailable` / `Failed initializing scaling graph`
   所以字体目录里只放 7 个必需字体。

★ 字体是**整个档案库共用**的，不跟 IP 走
=========================================
同一个工作室出片，字幕字体是「工作室的资产」，不是「某个客户的资产」。
放在每个 IP 里各存一份 39MB 的副本，50 个客户就是 50 份 2GB，
而且改一次字体要改 50 处 —— 迟早有一处漏了，出来一支字形不对的片子还不报错。

查找顺序（第一个存在的就用）：
  1. 环境变量 `SUB_FONTS_DIR`（临时试验/单条片子走别的字体时给）
  2. `<档案库>/_共用/字体_字幕标题/`   ← 正常情况都是这个
  3. `<IP>/02_素材真值/字体_字幕标题/`  ← 老档案的遗留位置，留着不报错
  4. 都没有 → **直接报错停下**，不退到系统字体目录
     （系统里装了 3000+ 字，libass 吃不下，退过去只会得到一支字形全错的片子）

用法:
  burn_ass.py <输入mp4> <字幕ass> <输出mp4> [--crf 18] [--preset slow]
  burn_ass.py <输入mp4> <字幕ass> <输出mp4> --preview 12.5 /tmp/p.png   # 抽单帧预览
"""
import os
import subprocess
import sys

APP = "/var/lib/flatpak/app/org.kde.kdenlive/current/active/files"
RT = "/var/lib/flatpak/runtime/org.kde.Platform/x86_64/6.11/active/files"
LIB = f"{APP}/lib:{APP}/lib/x86_64-linux-gnu:{RT}/lib:{RT}/lib/x86_64-linux-gnu"
LOADER = f"{RT}/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
FFMPEG = f"{APP}/bin/ffmpeg"

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_SUBDIR = "字体_字幕标题"


def find_fonts_dir():
    """按文档里的顺序找字体目录；找不到就抛错，绝不静默换字体。"""
    cands = [os.environ.get("SUB_FONTS_DIR") or "",
             # <档案库>/_共用/字体_字幕标题 —— HERE 是 <IP>/03_生产脚本
             os.path.normpath(os.path.join(HERE, "..", "..", "_共用", FONT_SUBDIR)),
             os.path.normpath(os.path.join(HERE, "..", "02_素材真值", FONT_SUBDIR))]
    for d in cands:
        if d and os.path.isdir(d) and any(
                f.lower().endswith((".ttf", ".otf", ".ttc")) for f in os.listdir(d)):
            return d
    raise SystemExit(
        "找不到字幕字体目录。查过：\n  " + "\n  ".join(x for x in cands if x) +
        "\n\n字体是全库共用的，应该在 <档案库>/_共用/字体_字幕标题/ 里。\n"
        "临时换字体用环境变量 SUB_FONTS_DIR=/某目录。\n"
        "⚠ 不要指到系统字体目录（3000+ 个字体，libass 会把内存吃爆，"
        "而且它只会静默回退 Noto —— 片子看起来正常，字形全错。）")


FONTS = find_fonts_dir()

ENV = {"LD_LIBRARY_PATH": LIB, "PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}


def run(args, **kw):
    return subprocess.run([LOADER, "--library-path", LIB, FFMPEG] + args,
                          env=ENV, capture_output=True, **kw)


def escape_path(p):
    """ffmpeg 滤镜参数里 `:` 和 `\\` 必须转义（盘符/路径问题）。"""
    return p.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def vf_for(ass, pre=None):
    """pre：烧字幕之前先跑的滤镜链（如曝光校正 curves）。

    ⚠️ 顺序不能反：`curves` 必须在 `subtitles` **之前**。
    字幕是白字 + 深棕描边，如果先烧字幕再压曲线，
    白字（1.0）会被压到 0.83、描边一起变浅，字幕对比度全毁。
    正确顺序 = 先修画面 → 再烧字。
    """
    v = f"subtitles='{escape_path(ass)}':fontsdir='{escape_path(FONTS)}'"
    return f"{pre},{v}" if pre else v


def burn(src, ass, dst, crf=18, preset="slow", audio_bitrate="192k", pre=None):
    args = ["-v", "error", "-i", src,
            "-vf", vf_for(ass, pre),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart", "-y", dst]
    r = run(args)
    if r.returncode != 0:
        raise SystemExit("烧录失败：\n" + r.stderr.decode()[:1500])
    return dst


def preview(src, ass, t, out, pre=None):
    r = run(["-v", "error", "-i", src, "-vf", vf_for(ass, pre),
             "-ss", str(t), "-frames:v", "1", "-update", "1", "-y", out])
    if r.returncode != 0:
        raise SystemExit("预览失败：\n" + r.stderr.decode()[:1200])
    return out


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    src, ass, dst = a[0], a[1], a[2]
    # --curve "<curves参数>"：烧字幕的同一次编码里顺带做曝光校正，
    # 省掉中间文件的二次压缩（画质只损失一次）
    # --grade "<完整滤镜链>"：需要 gamma + curves 组合时用这个（--curve 只能给纯 curves）
    pre = None
    if "--grade" in sys.argv:
        pre = sys.argv[sys.argv.index("--grade") + 1]
    elif "--curve" in sys.argv:
        c = sys.argv[sys.argv.index("--curve") + 1]
        pre = f"curves=all='{c}'"
    if "--preview" in sys.argv:
        i = sys.argv.index("--preview")
        preview(src, ass, sys.argv[i + 1], sys.argv[i + 2], pre)
        print("预览:", sys.argv[i + 2])
        return
    crf = int(sys.argv[sys.argv.index("--crf") + 1]) if "--crf" in sys.argv else 18
    preset = sys.argv[sys.argv.index("--preset") + 1] if "--preset" in sys.argv else "slow"
    print("字体目录:", FONTS)
    if pre:
        print("同步曝光校正:", pre)
    print("烧录中…", src, "→", dst)
    burn(src, ass, dst, crf, preset, pre=pre)
    n = os.path.getsize(dst) / 1024 / 1024
    print(f"完成 {dst}  {n:.1f} MB")


if __name__ == "__main__":
    main()
