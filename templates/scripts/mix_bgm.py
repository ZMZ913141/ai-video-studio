#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把配乐垫底混进成片，并把整片响度归一到平台标准。

三段各自解决一件事，顺序不能乱
==============================
1. **人声响度对齐（VO_NORM）** —— 修的是「TTS 每次重跑电平不一样」
2. **人声处理链（VO_FX）**     —— 修的是「句尾塌下去听不清」
3. **整片响度归一（loudnorm）** —— 修的是「整个系列比平台标准低 10 LU」

①②③ 必须按这个顺序：压缩器的 threshold 是绝对电平，
所以人声必须先对齐到固定基准再压；而整片归一必须放在最后，
否则配乐一进来就把人声的比例算歪了。

------------------------------------------------------------------
① 人声响度对齐（默认开）
------------------------------------------------------------------
TTS 每次重跑的整体电平是随机的：S01 −28.5 / S02 −27.6 / S03 −26.8，
而 S04 首版重跑出来是 −30.5 —— 比全系列低 3.7 dB。直接混进去，
配乐相对人声反而更靠前，`audio_check.py` 会判 ❌「基本无配乐」。

所以混音前先量人声 RMS，按 `VO_TARGET`（默认 −26.8，S03 定板）算增益，
钳在 ±8 dB 内再施加。关掉：`VO_NORM=0`。手动指定：`VO_GAIN=+3.7`。

------------------------------------------------------------------
② 人声处理链（默认开）—— 2026-10-28 用户反馈「尾音的声太轻」后加的
------------------------------------------------------------------
实测 S04 逐条 cue 的 句首/句中/句尾 电平，**句尾比句中低 5～9 dB**
（cue13 −9.0、cue15 −9.3、cue18 −9.3）。中文的信息重心常在句尾，
句尾一塌，没字幕就听不出在说什么。

链：`highpass 80` → `−1.5dB @250Hz`（去浑浊，200-500Hz 占了近 50% 能量）
    → `+2.5dB @3.2kHz`（抬辅音，提可懂度）
    → `acompressor(-28dB, ratio 5, knee 6, 15/250ms)`

压缩器是抬句尾的**唯一有效手段**：低电平的句尾本来就在阈值以下不压，
高电平的句首被压掉，动态从 13 dB 收到 6 dB，句尾就相对浮上来了。
⚠️ 注意 **makeup 抬不了句尾** —— makeup 是对所有电平线性相加，
相对关系一点不变。真正改变相对关系的只有 ratio。
关掉：`VO_FX=0`。

------------------------------------------------------------------
③ 整片响度归一（默认开）—— 全系列最根本的问题
------------------------------------------------------------------
EBU R128 实测四条成片：S01 −28.8 / S02 −27.1 / S03 −26.4 / S04 −25.8 LUFS。
**短视频平台标准是 −14～−16 LUFS，我们低了 10～12 LU。**
之前只用 RMS 对齐（−26.8 dBFS），RMS 和 LUFS 不是一回事，
前者不代表人耳感受到的响度，所以系列一直「量着正常、听着很轻」。

现在改用 `loudnorm` 两遍模式（先测再应用，官方推荐做法）：
目标 `I=−16 LUFS / TP=−1.5 dBTP / LRA=11`。
参数：`LUFS_I` `LUFS_TP` `LUFS_LRA`；关掉：`LUFS_NORM=0`。

用法
====
  mix_bgm.py <视频> <配乐wav> <输出mp4> [配乐比人声低几 LU]
  VO_NORM=0/1  VO_TARGET=-26.8  VO_GAIN=+3.7  VO_FX=0/1
  BGM_ABS_DB=-12（强制绝对增益，跳过自动）  BGM_DUCK=1/0  鸭子压缩
  LUFS_NORM=0/1  LUFS_I=-16  LUFS_TP=-1.5  LUFS_LRA=11

------------------------------------------------------------------
④ 配乐电平（2026-10-28 晚用户反馈「BGM 大声、人声小，相反了」后重做）
------------------------------------------------------------------
原来第 4 个参数是**固定绝对增益**（-6 dB），这个做法从根上就不对：
它假设「所有配乐的响度差不多」，而实际素材差得远。
实测 S05：人声 −27.7 LUFS，配乐 −12.3 LUFS —— 配乐本身比人声响 15.4 LU，
再按 -6 dB 垫进去，等于配乐还压人声 9.4 LU，整片响度被音乐主导，
归一之后人声反而更小。用户听到的就是「BGM 大声、人声小」。

现在改成**相对关系**：第 4 个参数是「配乐要比人声低多少 LU」（默认 13），
增益 = (人声 LUFS − 配乐 LUFS) − offset，两边都实测，跟素材响度无关。
再叠一层 sidechaincompress（鸭子压缩）：人一说话，配乐自动再退 6~9 dB，
句间自动回弹 —— 这是广播级做法，比单纯拉低配乐清楚得多。
"""
import json
import os
import re
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/"
      "lib/python3.13/site-packages/imageio_ffmpeg/binaries/"
      "ffmpeg-linux-x86_64-v7.0.2")

# ---- ② 人声处理链（阈值/比例见文件头的推导，别随手改）----
VO_CHAIN = os.environ.get("VO_CHAIN", (
    "highpass=f=80,"
    "equalizer=f=250:t=q:w=1.2:g=-1.5,"
    "equalizer=f=3200:t=q:w=1.4:g=+2.5,"
    "acompressor=threshold=-30dB:ratio=4:knee=8:attack=20:release=300"
))


def file_lufs(path):
    """量一个音频文件的整体响度（LUFS）。走 loudnorm 的测量模式，
    和最后那遍归一同一个算法口径 —— 不然「量的时候 -14、归一的时候 -16」这种
    口径不一致会让人以为哪里算错了。"""
    p = subprocess.run([FF, "-nostdin", "-hide_banner", "-nostats", "-i", path,
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r'\{\s*"input_i"[\s\S]*?\n\}', p.stderr)
    if not m:
        return None
    try:
        return float(json.loads(m.group(0))["input_i"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def resolve_bgm_gain(vid, bgm, offset_db):
    """返回 (增益dB, 说明)。配乐相对人声垫底，不用固定绝对值。

    为什么要自动：配乐素材的响度差得很远（S05 实测 −12.3 LUFS，
    有些舒缓钢琴只有 −24）。固定 -6 dB 在响素材上等于没垫，在轻素材上等于没有配乐。
    人声每套也不一样（−30.5 ~ −26.8），所以两边都得实测。
    """
    if os.environ.get("BGM_ABS_DB"):
        g = float(os.environ["BGM_ABS_DB"])
        return g, f"BGM_ABS_DB 手动指定 {g:+.1f} dB（跳过自动）"
    vo = file_lufs(vid)
    bg = file_lufs(bgm)
    if vo is None or bg is None:
        g = -float(offset_db)
        return g, f"⚠ 量不到响度（人声 {vo} / 配乐 {bg}），退回固定 −{offset_db:g} dB"
    want = vo - float(offset_db)          # 配乐该落到的绝对响度
    g = max(-45.0, min(6.0, want - bg))
    return g, (f"人声 {vo:.1f} LUFS / 配乐 {bg:.1f} LUFS → 配乐压到 {want:.1f} LUFS"
               f"（低 {float(offset_db):g} LU），施加 {g:+.1f} dB")


def voice_rms(vid):
    """量人声音轨的 RMS（dBFS）。用 ffmpeg 解码成单声道 f32 直接算，
    避免 astats 的日志格式在不同版本间漂移。"""
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-i", vid, "-map", "0:a:0",
                        "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return None
    a = np.frombuffer(p.stdout, dtype=np.float32)
    if a.size == 0:
        return None
    return float(20 * np.log10(max(float(np.sqrt((a.astype(np.float64) ** 2).mean())), 1e-9)))


def resolve_vo_gain(vid):
    """返回 (增益dB, 说明文本)。"""
    if os.environ.get("VO_NORM", "1") == "0":
        return 0.0, "VO_NORM=0 跳过响度对齐"
    if os.environ.get("VO_GAIN"):
        g = float(os.environ["VO_GAIN"])
        return g, f"VO_GAIN 手动指定 {g:+.1f} dB"
    tgt = float(os.environ.get("VO_TARGET", "-26.8"))
    rms = voice_rms(vid)
    if rms is None:
        return 0.0, "⚠ 量不到人声 RMS，跳过响度对齐"
    g = max(-8.0, min(8.0, tgt - rms))
    if abs(g) < 0.3:
        return 0.0, f"人声 {rms:.1f} dBFS（目标 {tgt:.1f}）→ 不调整"
    return g, f"人声 {rms:.1f} dBFS（目标 {tgt:.1f}）→ 施加 {g:+.1f} dB"


def loudnorm_measure(src, bgm, fc):
    """loudnorm 第一遍：只测量，不输出文件。返回 measured_* 字典。

    为什么必须两遍：单遍模式是「边测边改」，遇到响度起伏大的段落会跑偏；
    两遍（先测后应用 + linear=true）才是 ffmpeg 官方推荐的做法。
    """
    I = os.environ.get("LUFS_I", "-16")
    TP = os.environ.get("LUFS_TP", "-1.5")
    LRA = os.environ.get("LUFS_LRA", "11")
    # ⚠ 两个坑都踩过：
    #   1) loudnorm 必须写进**同一个** filter_complex，不能用 `-af` 追加 ——
    #      ffmpeg 报「Simple and complex filtering cannot be used together」。
    #   2) filter_complex 的输出必须显式 `-map`，ffmpeg 7 起不再自动映射，
    #      不写就是「amix has an unconnected output」。
    fg = fc + f";[mix]loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json[meas]"
    # ⚠ 两个输入都要给：滤镜链里引用了 [1:a]（配乐），只给 -i src 会报
    #   "Invalid file index 1"，而报错又被 try/except 吞掉，看起来像「测量失败」。
    p = subprocess.run([FF, "-nostdin", "-hide_banner", "-nostats", "-i", src, "-i", bgm,
                        "-filter_complex", fg, "-map", "[meas]",
                        "-f", "null", "-"], capture_output=True, text=True)
    if p.returncode != 0:
        print("  ⚠ loudnorm 测量退出码非 0：", p.stderr.strip()[-300:])
    # JSON 块在 stderr 里，但**不在末尾**——后面还跟着 `[out#0/null ...]` 那行，
    # 所以不能用 `$` 锚定（第一次写就栽在这里：正则不匹配 → 静默退回单遍模式）。
    m = re.search(r'\{\s*"input_i"[\s\S]*?\n\}', p.stderr)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def main():
    vid, bgm, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    offset = sys.argv[4] if len(sys.argv) > 4 else "13"

    vo_gain, why = resolve_vo_gain(vid)
    print("  ① 人声对齐:", why, flush=True)

    use_fx = os.environ.get("VO_FX", "1") != "0"
    vo_fx = (VO_CHAIN + ",") if use_fx else ""
    print("  ② 人声处理:", "压缩抬句尾 + 去浑浊 + 抬辅音" if use_fx else "VO_FX=0 跳过",
          flush=True)

    bgm_gain, bgm_why = resolve_bgm_gain(vid, bgm, offset)
    print(f"  ②b 配乐电平: {bgm_why}（{bgm_gain:+.1f} dB）", flush=True)

    # 鸭子压缩：人声一说话，配乐再退一截；说话间隙回弹。
    # 阈值 0.05 ≈ −26 dBFS，正好是人声的句子电平；ratio 4 约退 6~9 dB。
    # ⚠ 必须 asplit：同一路音频不能被两个消费者直接引用，ffmpeg 会报
    #   "Filter has an unconnected output" 或者干脆只喂给第一个。
    duck = os.environ.get("BGM_DUCK", "1") != "0"
    if duck:
        # ⚠ 只有人声要 asplit（一份进 amix、一份当 sidechain 键）。
        #   配乐**不能**乱 asplit：多切出来的那一路没人消费，ffmpeg 直接报
        #   "Filter asplit has an unconnected output" 并拒绝整个图 —— 2026-10-28 踩过。
        fc = (
            f"[1:a]volume={bgm_gain:+.1f}dB,highpass=f=90,lowpass=f=11000[bg];"
            # ① 先对齐到固定基准 → ② 再做压缩（阈值是绝对值，顺序反了压缩量就漂）
            f"[0:a]volume={vo_gain:+.1f}dB,{vo_fx}highpass=f=75,asplit=2[vo][vok];"
            f"[bg][vok]sidechaincompress=threshold=0.05:ratio=4:attack=15:release=400"
            f":makeup=1[bgd];"
            f"[vo][bgd]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]"
        )
        print("  ②c 鸭子压缩: 开（人声一响配乐自动退）", flush=True)
    else:
        fc = (
            f"[1:a]volume={bgm_gain:+.1f}dB,highpass=f=90,lowpass=f=11000[bg];"
            f"[0:a]volume={vo_gain:+.1f}dB,{vo_fx}highpass=f=75[vo];"
            f"[vo][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]"
        )
        print("  ②c 鸭子压缩: 关", flush=True)

    lufs = os.environ.get("LUFS_NORM", "1") != "0"
    ln = ""
    if lufs:
        I = os.environ.get("LUFS_I", "-16")
        TP = os.environ.get("LUFS_TP", "-1.5")
        LRA = os.environ.get("LUFS_LRA", "11")
        m = loudnorm_measure(vid, bgm, fc)
        if m:
            ln = (f"loudnorm=I={I}:TP={TP}:LRA={LRA}:linear=true"
                  f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                  f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                  f":offset={m['target_offset']}")
            print(f"  ③ 响度归一: 实测 {m['input_i']} LUFS → 目标 {I} LUFS"
                  f"（偏移 {float(m['target_offset']):+.1f} LU）", flush=True)
        else:
            ln = f"loudnorm=I={I}:TP={TP}:LRA={LRA}"
            print(f"  ③ 响度归一: 测量失败，退回单遍模式（目标 {I} LUFS）", flush=True)
    else:
        print("  ③ 响度归一: LUFS_NORM=0 跳过", flush=True)

    # ⚠ 归一化后再挂 alimiter 会**反而把电平顶上去**：alimiter 默认 level=true
    #   会先自动把输入电平抬到 limit 附近，实测解码后样本峰值冲到 +1.9 dBFS（削波）。
    #   loudnorm 的 TP= 本身就是真峰值限制器，归一化后不需要再限幅。
    full = fc + (f";[mix]{ln}[a]" if ln else
                 f";[mix]alimiter=limit=0.95:level=disabled[a]")
    cmd = [FF, "-nostdin", "-y", "-i", vid, "-i", bgm,
           "-filter_complex", full, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", dst]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        print(p.stderr.decode()[-1200:])
        sys.exit(1)
    print("OK", dst, os.path.getsize(dst), flush=True)


if __name__ == "__main__":
    main()
