#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
studio_algo —— 自适应算法层（引擎侧，纯函数，零副作用）

为什么要有这一层
================
以前这些公式散落在每个 IP 的 `03_生产脚本/gen_video.py` 副本里。
接 2 个 IP 时无所谓，接 50 个 IP 就是 50 份副本：
改一个安全线要改 50 处，漏一处就是一次 OOM 或一批废片。

所以算法（怎么算）抽回引擎，IP 只留参数（算什么档位）。
脚本副本里仍然各有一份实现（那是它们的运行时），但**真值的定义在这里**，
`vstudio algo` 可以直接预览，`scripts-diff` 也能拿它当基准去比偏离。

三条硬规律
==========
1. 段长 L = 5 + 17n        （H3 采样器的 latent 交接公式，改不得）
2. 切点 c_i = 17 × Σn      （L≡5 与 重叠≡5 的 5i 恰好抵消，所以切点必然是 17 的倍数）
3. 单段帧上限 ∝ 1/面积      （显存 ∝ 面积×帧数，分辨率涨了必须缩短段）
"""
import math

FPS = 24
STEP = 17                 # 段长 L = 5 + 17n
BASE_MP = 1.008           # 768x1376 —— 安全线的标定基准，不是出片档
BASE_MAXLEN = 192         # 1.008MP 下 16GB 卡的实测安全帧数（约 12.6GiB 峰值）
MULTIPLE = 32             # 分辨率吸附步长


# ---------------- 分辨率 ----------------
def calc_res(mp, multiple=MULTIPLE):
    """复刻 ResolutionSelector：9:16 面积不变，边长吸附到 multiple 的倍数。"""
    tp = mp * 1024 * 1024
    scale = math.sqrt(tp / (9 * 16))
    w = round(9 * scale / multiple) * multiple
    h = round(16 * scale / multiple) * multiple
    return int(w), int(h)


def real_mp(mp):
    """名义 MP 与真实 MP 会差（吸附到 32 的倍数造成的）。
    安全线必须按**真实**面积算 —— 名义 1.22 实际 1.2393，
    按名义算出的 MAXLEN=158 峰值 195.8，会越过 192 的实测安全线。"""
    w, h = calc_res(mp)
    return w * h / 1024 / 1024


def maxlen_for_mp(mp):
    """单段帧数上限（已是 5+17n 合法值）。"""
    rm = real_mp(mp)
    if rm <= BASE_MP * 1.02:
        return BASE_MAXLEN
    budget = BASE_MAXLEN * (BASE_MP / rm)        # 面积反比
    n = int((budget - 5) // STEP)
    return 5 + STEP * max(1, n)


def is_fast(mp):
    """< ~0.9MP 一律算快速档（产物必须带标记，防止被当成品交付）。"""
    return mp < BASE_MP * 0.9


# ---------------- 帧数网格 ----------------
def snap_frames(n, up=True, lo=5, hi=None):
    """把任意帧数吸附到 5+17n 网格。up=True 向上（宁长勿短，短了音频会缺尾）。"""
    if up:
        n = 5 + STEP * max(0, math.ceil((n - 5) / STEP))
    else:
        n = 5 + STEP * max(0, round((n - 5) / STEP))
    if hi:
        n = min(n, hi)
    return max(lo, n)


def total_frames(audio_sec, fps=FPS):
    """音频时长 → 总帧数。向上取整到 5+17n，且至少不短于音频。"""
    need = int(audio_sec * fps) + 1
    return snap_frames(need, up=True)


def min_segs(total, maxlen, overlap=5):
    """覆盖 total 帧最少要几段：n·MAXLEN − (n−1)·重叠 ≥ total"""
    n = 1
    while n * maxlen - (n - 1) * overlap < total:
        n += 1
    return n


def seg_grid(total, maxlen, overlap=5, target_segs=None, target_seg_secs=8.0):
    """按「17 格数」一步算出分段 —— 不需要搜索，也不需要吸附。

    恒等式：段长 L=5+17n、重叠 5 ⇒ 切点 c_i = ΣL − 5i = 17·Σn
    所以只要把总格数 gn=(total−5)/17 拆成 k 份，切点天生落在 17 的倍数上。

    返回 (格数list, 切点list, 段长list)
    """
    assert (total - 5) % STEP == 0, f"total={total} 不在 5+17n 网格上"
    gn = (total - 5) // STEP                      # 总格数
    n_max = (maxlen - 5) // STEP                  # 单段最大格数
    if n_max < 1:
        raise ValueError(f"MAXLEN={maxlen} 太小，连一段都排不下（需 ≥{5+STEP}）")

    k_min = min_segs(total, maxlen, overlap)
    if target_segs:
        k = max(k_min, int(target_segs))
    else:
        k = max(k_min, round(total / FPS / target_seg_secs) or 1)

    # 均分格数；若单段超过上限就继续加段（低分辨率档 n_max 反而更大，通常一次就过）
    base, rem = divmod(gn, k)
    while base > n_max:
        k += 1
        base, rem = divmod(gn, k)
    k = max(k, 1)

    ns = [base + (1 if i < rem else 0) for i in range(k)]
    ns = [x for x in ns if x > 0] or [1]
    cuts, acc = [], 0
    for x in ns[:-1]:
        acc += x
        cuts.append(acc * STEP)
    lens = [5 + STEP * x for x in ns]
    return ns, cuts, lens


# ---------------- 音频留白 ----------------
def audio_pads(sec):
    """音频前后各留多少白（秒）。

    为什么要有：TTS 第一个字常常贴着 0.00s 起，最后一字贴着结尾断，
    听起来「仓促」——开头没有呼吸，结尾没有落地。加固定的前后留白即可，
    SRT 是在**留白之后**的音频上做的 ASR，所以时间轴自动带上偏移，不用再挪字幕。

    自适应：长片尾部留多一点（收束感），短片少一点（不然留白占比过大）。
    钳位是为了不让留白喧宾夺主：整片 40s 时总留白约 1.0s（2.5%）。
    """
    head = min(0.45, max(0.25, 0.18 + 0.004 * sec))
    tail = min(1.00, max(0.55, 0.42 + 0.006 * sec))
    return round(head, 3), round(tail, 3)


def bgm_offset(sec):
    """配乐要比人声**低多少 LU**（不是绝对增益）。

    2026-10-28 晚改：原来是固定 -6 dB 的绝对增益，前提是「配乐响度都差不多」，
    而实际素材从 -12 到 -24 LUFS 都有。S05 就是 配乐 -12.3 / 人声 -27.7，
    垫 -6 之后配乐还压人声 9 LU，用户听到的是「BGM 大声、人声小」。
    现在只声明相对关系，绝对增益由 mix_bgm 实测两边响度后反算。

    2026-10-30 再调：上面那个相对关系**当时是虚的**——配乐增益拿「处理前」的
    人声算，而人声处理链（acompressor）会自己吃掉 10 dB，结果 T01 名义「低 13 LU」
    实际只低 5.3 dB（说话段 −14.9 / 停顿段 −20.3），用户直接反馈「BGM 影响到人声」。
    mix_bgm 已改成按处理后的人声算，13 现在是货真价实的 13（实测 13.9 dB 差）。
    顺手整体上调 2 LU：专业口播的配乐一般在人声下方 15~20 dB，
    13 LU 是能听懂的下限，不是舒适区。
    """
    g = 15.0
    if sec > 90:
        g = 19.0
    elif sec > 45:
        g = 17.0
    return g


# ---------------- 一次算全套 ----------------
def plan(audio_sec, mp=1.26, target_segs=None, target_seg_secs=8.0, overlap=5):
    """给定音频时长与出片档位，一次算出所有自适应参数。"""
    w, h = calc_res(mp)
    maxlen = maxlen_for_mp(mp)
    total = total_frames(audio_sec)
    ns, cuts, lens = seg_grid(total, maxlen, overlap,
                              target_segs=target_segs,
                              target_seg_secs=target_seg_secs)
    head, tail = audio_pads(audio_sec)
    return {
        "audio_sec": round(audio_sec, 2),
        "mp": mp,
        "res": [w, h],
        "real_mp": round(real_mp(mp), 4),
        "fast": is_fast(mp),
        "maxlen": maxlen,
        "overlap": overlap,
        "total_frames": total,
        "total_sec": round(total / FPS, 3),
        "segs": len(ns),
        "grid": ns,
        "cuts": cuts,
        "cuts_sec": [round(c / FPS, 2) for c in cuts],
        "seg_len": lens,
        "seg_sec": [round(L / FPS, 2) for L in lens],
        "pad_head": head,
        "pad_tail": tail,
        "padded_sec": round(audio_sec + head + tail, 2),
        "bgm_offset": bgm_offset(audio_sec),
    }


if __name__ == "__main__":
    import json
    import sys
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    mp = float(sys.argv[2]) if len(sys.argv) > 2 else 1.26
    ts = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(json.dumps(plan(sec, mp, target_segs=ts), ensure_ascii=False, indent=2))
