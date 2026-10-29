#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ASS 字幕烧进成片（用 Kdenlive flatpak 自带的 ffmpeg + libass）。

为什么需要这个包装器
==================
1. `flatpak run` 在本沙箱必死（xdg-dbus-proxy 握手失败），所以直接调 app 里的二进制。
2. 必须用 runtime 的 ld.so 显式加载（`--library-path`），且 `LD_LIBRARY_PATH`
   只能通过 `env` 给目标进程，否则宿主的 bash/head 会被 GLIBC 2.43 的库带崩。
3. **字体走系统 fontconfig**（不传 `fontsdir`）：
   - 不给 fontsdir：libass 自己查 fontconfig → 系统装了就能找到（实测与给字体目录逐字节一致）
   - 给大目录（3000+ 字体）：内存爆到 5.1GB（沙箱 4GB 上限）→
     `Resource temporarily unavailable` / `Failed initializing scaling graph`
   - 给空目录：静默回退 Noto（字形全错还不报错）
   → 所以**不给 fontsdir**，但**烧之前必须验字体真的在系统里**（见 `check_fonts`）。

★ 字体是**系统级资产**，不是某个客户、也不是「全库共用目录」的资产
==========================================================
2026-10-29 实测（.cache/fonttest）：
  A 给 <档案库>/_共用/字体_字幕标题/  → 119MB 内存，输出与 B 逐字节一致
  B 不给 fontsdir，走系统 fontconfig   →  68MB 内存，输出与 A 逐字节一致 ✓
  C 给系统字体目录（3005 个）           → 5.1GB 内存（爆）✗
  D 不存在的字体名                      → 静默回退，字形不同（这就是「字形全错」的真相）
结论：字体已装进 ~/.local/share/fonts/ 并注册进 fontconfig，**不需要再存一份副本**。
字体名在 ip.json 的 subtitle.font_cue / font_cue_b / font_title，
换字体 = 改 ip.json 里的名字（系统里没装就先装进系统），不用碰任何目录。

查找顺序：
  1. 环境变量 `SUB_FONTS_DIR`（临时试验/单条片子走别的字体时给；给了就用它当 fontsdir）
  2. 不给 → 走系统 fontconfig（正常情况都是这个）

用法:
  burn_ass.py <输入mp4> <字幕ass> <输出mp4> [--crf 18] [--preset slow]
  burn_ass.py <输入mp4> <字幕ass> <输出mp4> --preview 12.5 /tmp/t.png   # 抽单帧预览
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

# SUB_FONTS_DIR：临时试验/单条片子换字体时用。给了就当 fontsdir 给 libass。
# 不给 = 走系统 fontconfig（正常情况）。
OVERRIDE_DIR = os.environ.get("SUB_FONTS_DIR") or ""


def fc_match(family):
    """问 fontconfig 这个家族名能不能解析到真实字体文件。解析不到就返回空。"""
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}\n", family],
                           capture_output=True, text=True, timeout=10)
        out = (r.stdout or "").strip().splitlines()
        return out[0] if out else ""
    except Exception:
        return ""


def ass_font_names(ass_path):
    """从 ASS 的 Style 行里抠出所有 Fontname（去重、保持顺序）。"""
    names, seen = [], set()
    try:
        with open(ass_path, encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s.lower().startswith("style:"):
                    continue
                parts = s.split(",")
                if len(parts) < 2:
                    continue
                nm = parts[1].strip()
                if nm and nm not in seen:
                    seen.add(nm)
                    names.append(nm)
    except Exception:
        pass
    return names


def check_fonts(ass_path):
    """烧之前验字体：ASS 里每个字体名都必须在系统里真的能解析到。

    为什么非要有这道：字体名解析不到时 libass **不报错**，静默换成默认字体，
    片子照出、字形全错 —— 这类废片最难查（要一帧帧比对才发现）。
    宁可在这里停下，也不要交一支字形不对的片子出去。
    """
    bad = []
    for nm in ass_font_names(ass_path):
        path = fc_match(nm)
        if not path:
            bad.append((nm, "系统里没有这个字体家族"))
            continue
        # fc-match 找不到时会给一个「随便哪个都行」的结果（通常 DejaVu/Noto）。
        # 认不出来时它返回的 family 不含我们要的字 → 用文件名反查确认。
        try:
            base = os.path.basename(path)
            r = subprocess.run(["fc-scan", "-f", "%{family}\n", path],
                               capture_output=True, text=True, timeout=10)
            fams = [x.strip() for x in (r.stdout or "").split(",") if x.strip()]
            # 有些家族名的全名在 style 里，但 fc-scan 只报部分名 → 用「包含」判断
            if not any(nm in f or f in nm for f in fams) and nm not in fams:
                bad.append((nm, f"解析到了 {base}，但不是这个字体"))
        except Exception:
            pass
    return bad


def find_fonts_dir():
    """返回要传给 libass 的 fontsdir；空串 = 走系统 fontconfig。

    2026-10-29 实测：不给 fontsdir 的输出与给精简字体目录**逐字节一致**，
    内存还少一半（68MB vs 119MB）。而给系统字体目录（3000+ 个）会吃到 5.1GB。
    所以默认不给 fontsdir，字体装在系统里就行。
    """
    if OVERRIDE_DIR:
        if not os.path.isdir(OVERRIDE_DIR):
            raise SystemExit(
                f"SUB_FONTS_DIR 指向的目录不存在：{OVERRIDE_DIR}\n"
                "（不给这个环境变量就是走系统 fontconfig，正常情况不用设）")
        return OVERRIDE_DIR
    return ""


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
    v = f"subtitles='{escape_path(ass)}'"
    if FONTS:
        v += f":fontsdir='{escape_path(FONTS)}'"
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
        bad = check_fonts(ass)
        if bad:
            print("✗ 字幕里有字体在系统里解析不到：", file=sys.stderr)
            for nm, why in bad:
                print(f"    {nm} —— {why}", file=sys.stderr)
            sys.exit(1)
        preview(src, ass, sys.argv[i + 1], sys.argv[i + 2], pre)
        print("预览:", sys.argv[i + 2])
        return
    crf = int(sys.argv[sys.argv.index("--crf") + 1]) if "--crf" in sys.argv else 18
    preset = sys.argv[sys.argv.index("--preset") + 1] if "--preset" in sys.argv else "slow"
    print("字体来源:", FONTS if FONTS else "系统 fontconfig")
    # ★ 先验字体再烧：解析不到时 libass 不报错，会静默换成别的字体，
    #   片子照出但字形全错 —— 宁可现在停下。
    bad = check_fonts(ass)
    if bad:
        print("✗ 字幕里有字体在系统里解析不到：", file=sys.stderr)
        for nm, why in bad:
            print(f"    {nm} —— {why}", file=sys.stderr)
        print("\n装进系统即可（字体不是某个客户的东西，是系统的）：\n"
              "  cp 字体.ttf ~/.local/share/fonts/ && fc-cache -f\n"
              "或者换个系统里已有的字体名（改 ip.json 的 subtitle.font_*）。",
              file=sys.stderr)
        sys.exit(1)
    if pre:
        print("同步曝光校正:", pre)
    print("烧录中…", src, "→", dst)
    burn(src, ass, dst, crf, preset, pre=pre)
    n = os.path.getsize(dst) / 1024 / 1024
    print(f"完成 {dst}  {n:.1f} MB")


if __name__ == "__main__":
    main()
