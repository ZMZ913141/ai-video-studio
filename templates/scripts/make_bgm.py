#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配乐生成 —— 四条路挑一条，不再是「只有那一首」

为什么改成可挑
--------------
最早只有 `synth`（本地加法合成 88 BPM 温暖器乐）一条路，
当时的理由是「本机没有可用音乐生成模型」。这句话在本文件旧版本里写着，
★ 但它在 2026-10-29 已经被证伪：本机 ComfyUI 里 MiniMax-Music-3 的
  DiT / 文本编码器 / VAE **三件套齐全**，ComfyUI 也注册了对应节点
  （MiniMaxMusic3TextEncode + EmptyMiniMaxMusic3LatentAudio + VAEDecodeAudio）。
  旧注释里说「YuE2MusicPromptEnhancer 只输出 5 个 String」是真的，
  但那是**提示词增强节点**，不是采样节点 —— 当年把它当成了「本机不能生音乐」的证据。

四条路（source）
--------------
  synth    本地合成。稳、快（几秒）、不占 GPU、每次一样。缺点：只有这一首。
  comfy    走 ComfyUI 用 MiniMax-Music-3 生成。想要不同风格和段落变化时用。
           占 GPU + 显存，几十秒到几分钟，**用前建议先看 memory 里那个
           「出片前查 MemAvailable」的规矩**。
  reuse    ★ 复用配乐库里已有的一首（2026-10-30 加）。**不重新生成**，
           只是按本片时长裁一下，几秒完事、不占 GPU。
           配乐库 = <IP>/02_素材真值/配乐/，自己丢曲子进去也认。
  library  从另一个外部曲库目录挑（可随机、可指定）。接客户的老曲子、已买断的乐库走这条。
  online   从在线免费曲库下载（需对应平台的 key）。目前支持 jamendo / freesound。

★ 为什么要有 reuse：配乐是「生成一次、用很多次」的东西。以前每次出片都重跑
  一遍生成（synth 还好，comfy 一次几十秒到几分钟还占显卡），出的还是差不多的东西。
  现在**任何一路生成完都会自动沉淀一份进配乐库**，之后把 source 切成 reuse 挑一首即可。

选哪条写在哪
------------
真源是 ip.json 的 `bgm` 段（引擎不放硬编码）：
    "bgm": {
      "source": "reuse",                 # synth | comfy | reuse | library | online
      "prompt": "warm minimal guitar bed, calm, no drums",
      "seed": 20261027,
      "pick": "synth-20261027",          # ★ reuse/library 挑哪首（文件名子串）
      "library_dir": "/你的/配乐库",       # source=library 时必填
      "random": false,                   # library 随机挑
      "online": {"provider": "jamendo", "api_key": "", "query": "ambient"},
      "sampler": {"steps": 20, "cfg": 1.0, "scheduler": "simple", "top_k": 50}
    }
环境变量 SOURCE / BGM_PROMPT / BGM_SEED / BGM_PICK 可临时覆盖；
NO_ARCHIVE=1 可以不让这次生成的曲子进配乐库。

用法
----
  python3 make_bgm.py <输出wav> [时长秒] [--source comfy] [--seed 123]
  DRY=1   只打印准备怎么做 + 将要提交给 ComfyUI 的模型名，不占 GPU
"""
import json
import os
import random
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
SR = 48000


# ══════════════════════════════════════════ 路 1：本地合成（原方案）
# ★ 节奏不该焊死：88 BPM 是「温暖抒情口播」的默认值，快节奏带货 / 严肃财经都不合适。
#   走 env BGM_BPM 覆盖，默认保持不变（已交付的片子不受影响）。
BPM = float(os.environ.get("BGM_BPM") or 88.0)
BEAT = 60.0 / BPM
BAR = 4 * BEAT
_SYNTH_SEED = 20261027
_rng = np.random.default_rng(_SYNTH_SEED)

CHORDS = [
    (0, [261.63, 329.63, 392.00, 523.25]),   # C
    (0, [261.63, 329.63, 392.00, 523.25]),
    (1, [196.00, 246.94, 293.66, 392.00]),   # G
    (1, [196.00, 246.94, 293.66, 392.00]),
    (2, [220.00, 261.63, 329.63, 440.00]),   # Am
    (2, [220.00, 261.63, 329.63, 440.00]),
    (3, [174.61, 220.00, 261.63, 349.23]),   # F
    (3, [174.61, 220.00, 261.63, 349.23]),
]
BASS = {0: 130.81, 1: 98.00, 2: 110.00, 3: 87.31}


def pluck(f, dur, amp=1.0):
    """指弹：快起 + 指数衰减 + 谐波叠加。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 3.2) * (1 - np.exp(-t * 420))
    s = (np.sin(2 * np.pi * f * t) * 1.0
         + np.sin(2 * np.pi * f * 2 * t) * 0.34 * np.exp(-t * 5)
         + np.sin(2 * np.pi * f * 3 * t) * 0.16 * np.exp(-t * 7)
         + np.sin(2 * np.pi * f * 4 * t) * 0.07 * np.exp(-t * 9))
    return s * env * amp


def bass(f, dur, amp=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 1.1) * (1 - np.exp(-t * 160))
    s = np.sin(2 * np.pi * f * t) + 0.18 * np.sin(2 * np.pi * f * 2 * t)
    return s * env * amp


def brush(dur, amp=1.0):
    """轻刷：带通噪声的短包络。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = _rng.normal(0, 1, n)
    x = np.diff(np.concatenate([[0.0], x]))
    x = np.convolve(x, np.ones(4) / 4, mode="same")
    env = np.exp(-t * 11) * (1 - np.exp(-t * 900))
    return x * env * amp


def build(total):
    n = int(total * SR)
    out = np.zeros(n, dtype=np.float64)
    bassline = np.zeros(n, dtype=np.float64)
    perc = np.zeros(n, dtype=np.float64)

    nbars = int(np.ceil(total / BAR)) + 1
    for b in range(nbars):
        t0 = b * BAR
        if t0 >= total:
            break
        _, freqs = CHORDS[b % len(CHORDS)]
        # 指弹分解和弦：每拍一个音，第 3 拍补一个高音
        pattern = [(0.0, freqs[0]), (1.0, freqs[2]), (2.0, freqs[1]), (3.0, freqs[3])]
        for beat, f in pattern:
            s = pluck(f, min(2.4, total - (t0 + beat * BEAT)), 0.30)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                out[i:e] += s[:e - i]
        # 贝斯：每小节根音，第 3 拍轻点一次
        for beat in (0.0, 2.5):
            s = bass(BASS[CHORDS[b % len(CHORDS)][0]], min(1.9, total - (t0 + beat * BEAT)), 0.5)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                bassline[i:e] += s[:e - i]
        # 轻刷在第 2、4 拍
        for beat in (1.0, 3.0):
            s = brush(0.30, 0.11)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                perc[i:e] += s[:e - i]

    mix = out + bassline + perc
    # 极轻的梳状"房间感"：短延迟叠加，避免干瘪
    d = int(0.075 * SR)
    wet = np.concatenate([np.zeros(d), mix[:-d]]) * 0.16
    mix = mix + wet
    # 柔和压限：tanh 软限幅
    mix = np.tanh(mix * 1.15) * 0.75
    fi, fo = int(0.9 * SR), int(1.4 * SR)
    if len(mix) > fi + fo:
        mix[:fi] *= np.linspace(0, 1, fi)
        mix[-fo:] *= np.linspace(1, 0, fo)
    return mix


def src_synth(total, cfg, log):
    log("本地合成 %g BPM 温暖器乐（不占 GPU）" % BPM)
    a = build(total)
    peak = np.max(np.abs(a)) or 1.0
    return (a / peak * 0.92).astype(np.float32), SR


# ══════════════════════════════════════════ 路 2：ComfyUI 生一条
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
# ★ 模型名差一个字符，ComfyUI 报的是「模型不可用」，看着像没下载过。
#   所以下面先拿 /object_info 核对一遍，名字不在列表里就明说，不让它在提交后才炸。
M3_MODELS = {
    "dit": os.environ.get("BGM_DIT", "MiniMax-Music-3/minimax_music3_dit_fp16.safetensors"),
    "clip": os.environ.get("BGM_CLIP", "Minimax-music-3/minimax_music3_text_encoder_bf16.safetensors"),
    "vae": os.environ.get("BGM_VAE", "minimax_music3_dav.safetensors"),
}
_LOADERS = {"dit": ("UNETLoader", "unet_name"),
            "clip": ("CLIPLoader", "clip_name"),
            "vae": ("VAELoader", "vae_name")}


def comfy_available(model_index=None):
    """返回 (能不能用, 缺什么)。用 /object_info 静态核对模型名，不占显存。"""
    try:
        req = urllib.request.Request(COMFY + "/api/object_info", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            oi = json.load(r)
    except Exception as ex:
        return False, f"连不上 ComfyUI（{COMFY}）：{type(ex).__name__}"

    missing = []
    for cls in ("MiniMaxMusic3TextEncode", "EmptyMiniMaxMusic3LatentAudio",
                "VAEDecodeAudio", "UNETLoader", "CLIPLoader", "VAELoader", "KSampler"):
        if cls not in oi:
            missing.append(f"缺节点 {cls}")
    for key, fn in M3_MODELS.items():
        cls, inp = _LOADERS[key]
        lst = (((oi.get(cls) or {}).get("input") or {}).get("required") or {}).get(inp)
        names = lst[0] if isinstance(lst, list) and lst and isinstance(lst[0], list) else []
        if fn not in names:
            missing.append(f"{cls} 列表里没有 {fn}")
    return (not missing), "；".join(missing)


def src_comfy(total, cfg, log):
    """提交 MiniMax-Music-3 工作流，等它出 WAV，回来作为 numpy 数组。

    为什么用 KSampler 而不是专用采样节点：本机没装耳朵类的专用音乐采样器，
    而 MiniMaxMusic3TextEncode 产出的就是标准 CONDITIONING + EmptyLatentAudio
    产出的就是标准 LATENT，通用 KSampler 可以直接吃。
    """
    ok, why = comfy_available()
    if not ok:
        raise RuntimeError(why)

    smp = cfg.get("sampler") or {}
    steps = int(smp.get("steps", 20))
    cfgk = float(smp.get("cfg", 1.0))
    sched = str(smp.get("scheduler", "simple"))
    top_k = int(smp.get("top_k", 50))
    seed = int(cfg.get("seed", 0) or 0)
    caption = str(cfg.get("prompt") or "").strip()
    if not caption:
        raise RuntimeError("走 ComfyUI 生音乐得先给一句描述（ip.json 的 bgm.prompt）")

    # 生成要比目标长一点，回来再裁 —— 直接用 target 时长容易尾部收不住
    gen_secs = float(total) + 6.0
    graph = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": M3_MODELS["dit"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": M3_MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": M3_MODELS["vae"]}},
        # 正向：真正想要的
        "4": {"class_type": "MiniMaxMusic3TextEncode",
              "inputs": {"clip": ["2", 0], "caption": caption, "lyrics": "",
                         "seed": seed, "max_duration": gen_secs,
                         "cfg_scale": 1.5, "top_k": top_k}},
        # 负向：空描述。CFG 已经内置在 TextEncode 里，这里只做兜底。
        "5": {"class_type": "MiniMaxMusic3TextEncode",
              "inputs": {"clip": ["2", 0], "caption": "", "lyrics": "",
                         "seed": seed, "max_duration": gen_secs,
                         "cfg_scale": 1.5, "top_k": top_k}},
        "6": {"class_type": "EmptyMiniMaxMusic3LatentAudio",
              "inputs": {"seconds": gen_secs, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "seed": seed, "steps": steps,
                         "cfg": cfgk, "sampler_name": "euler", "scheduler": sched,
                         "denoise": 1.0}},
        "8": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveAudio",
              "inputs": {"audio": ["8", 0], "filename_prefix": "bgm/gen_bgm_"}},
    }
    log(f"提交 ComfyUI MiniMax-Music-3：{steps} 步 / cfg {cfgk} / seed {seed} / 生成 {gen_secs:.0f}s")

    import time
    import uuid
    cid = str(uuid.uuid4())
    body = json.dumps({"prompt": graph, "client_id": cid}).encode()
    req = urllib.request.Request(COMFY + "/prompt",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            pid = json.load(r).get("prompt_id")
    except urllib.error.HTTPError as ex:
        raise RuntimeError("ComfyUI 拒收这条工作流：" + ex.read().decode("utf-8", "ignore")[:600])

    # 等它跑完
    deadline = time.time() + max(600, total * 12)
    bad = 0
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(COMFY + f"/history/{pid}", timeout=20) as r:
                h = json.load(r)
            bad = 0
        except Exception as ex:
            # ★ 服务挂了要报错，不能静默重试到 deadline（同 gen_tts 的坑，2026-10-30）
            bad += 1
            if bad >= 10:
                raise RuntimeError(f"连不上 ComfyUI（{COMFY}）：{type(ex).__name__}: {ex}"
                                   f"\n  配乐任务 {pid} 拿不到结果，已停止等待。"
                                   f"\n  先确认：curl -s --noproxy '*' {COMFY}/system_stats")
            continue
        item = (h or {}).get(pid) or {}
        outs = ((item.get("outputs") or {}).get("9") or {}).get("audio") or []
        if not outs:
            continue
        fn = outs[0].get("filename")
        sub = outs[0].get("subfolder") or ""
        q = {"filename": fn, "type": "output"}
        if sub:
            q["subfolder"] = sub
        url = COMFY + "/view?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=120) as r:
            raw = r.read()
        tmp = "/tmp/_bgm_comfy_" + os.path.basename(fn or "m.wav")
        with open(tmp, "wb") as f:
            f.write(raw)
        log(f"拿到 ComfyUI 产物：{os.path.basename(tmp)}")
        return _decode_file(tmp)

    raise RuntimeError("ComfyUI 那边超过时限还没出音频（可以在 ComfyUI 界面看队列是不是卡了）")


# ══════════════════════════════════════════ 路 3：本地曲库
AUDIO_EXT = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".opus")


def library_files(d):
    if not d or not os.path.isdir(d):
        return []
    out = []
    for root, _dirs, files in os.walk(d):
        for n in files:
            if n.lower().endswith(AUDIO_EXT) and not n.startswith("."):
                out.append(os.path.join(root, n))
    return sorted(out)


def src_library(total, cfg, log):
    d = cfg.get("library_dir") or os.environ.get("BGM_LIBRARY") or ""
    files = library_files(d)
    if not files:
        raise RuntimeError(
            f"曲库目录里没有音频：{d or '（没配）'}\n"
            f"    去 ip.json 的 bgm.library_dir 指一个目录，或直接换 --source synth / comfy。")

    pick = cfg.get("pick") or os.environ.get("BGM_PICK") or ""
    if pick:
        hit = [f for f in files if pick.lower() in os.path.basename(f).lower()]
        if not hit:
            raise RuntimeError(f"曲库里没有名字带「{pick}」的曲子（共 {len(files)} 首）")
        files = hit
    elif cfg.get("random"):
        files = [random.Random(int(cfg.get("seed") or 0)).choice(files)]

    chosen = files[0]
    log(f"曲库挑中：{os.path.basename(chosen)}" + (f"（共 {len(files)} 首候选）" if len(files) > 1 else ""))
    return _decode_file(chosen)


# ══════════════════════════════════════════ 路 5：复用配乐库（2026-10-30）
LIB_INDEX = "库.json"


def _lib_dir():
    """配乐沉淀目录 —— IP 自己的 02_素材真值/配乐/。

    为什么放 IP 自己的目录而不是全库共用：配乐跟人绑定（不同 IP 的画面气质、
    产品、节奏不一样，曲子不能共用），客户自带曲子更是「这个客户的东西」。
    """
    try:
        from ip_guard import load_ip
        root = getattr(load_ip(strict=False, verbose=False), "root", "") or ""
    except Exception:
        root = ""
    return os.path.join(root, "02_素材真值", "配乐") if root else ""


def _lib_meta(d):
    p = os.path.join(d, LIB_INDEX)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return {m.get("file"): m for m in (doc.get("items") or []) if m.get("file")}
    except Exception:
        return {}


def reuse_candidates(d):
    """配乐库里所有可复用的曲子。

    ★ 以**目录里的文件**为准、索引只做补充：索引丢了或被人手改坏了，
      曲子照样能用（只是少了「哪来的、什么提示词」这些说明）。
    """
    files = [f for f in library_files(d) if os.path.basename(f) != LIB_INDEX]
    meta = _lib_meta(d)
    out = []
    for f in files:
        n = os.path.basename(f)
        m = meta.get(n) or {}
        out.append({
            "path": f, "name": n,
            "source": m.get("source") or "", "prompt": m.get("prompt") or "",
            "seed": m.get("seed"), "seconds": m.get("seconds"),
            "created": m.get("created") or datetime.fromtimestamp(
                os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M"),
        })
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


def src_reuse(total, cfg, log):
    d = _lib_dir()
    if not d or not os.path.isdir(d):
        raise RuntimeError(
            "配乐库还是空的（还没生成过配乐）。\n"
            "    先跑一次 --source synth 或 comfy 生成一首，它会自动沉淀进配乐库，\n"
            "    之后就能一直复用了。")
    cands = reuse_candidates(d)
    if not cands:
        raise RuntimeError(f"配乐库目录里没有音频：{d}")

    pick = (cfg.get("pick") or os.environ.get("BGM_PICK") or "").strip()
    if pick:
        hit = [c for c in cands if pick.lower() in c["name"].lower()]
        if not hit:
            raise RuntimeError(
                f"配乐库里没有名字带「{pick}」的曲子（共 {len(cands)} 首）。\n"
                f"    库里现有：" + "、".join(c["name"] for c in cands[:6]))
        cands = hit
    one = cands[0]
    log(f"复用配乐库：{one['name']}"
        + (f"（{one['source']}"
           + (f" / {one['prompt'][:40]}" if one.get("prompt") else "")
           + f" / 种子 {one['seed']}）" if one.get("source") else ""))
    return _decode_file(one["path"])


def archive(arr, sr, source, cfg, log):
    """把这次生成的曲子沉淀进配乐库 —— 下次不用再生成，直接 reuse。

    文件名带来源和种子，人一眼能看出「这是哪次生的」；
    元信息进 库.json，界面列库时能显示提示词/种子/时长。
    """
    if (os.environ.get("NO_ARCHIVE") or "").strip().lower() in ("1", "true", "yes"):
        return None
    d = _lib_dir()
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{source}-seed{int(cfg.get('seed') or 0)}-{stamp}.wav"
        dst = os.path.join(d, name)
        p = subprocess.run([FF, "-nostdin", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1",
                            "-i", "-", "-ar", "48000", "-ac", "2", "-b:a", "192k", dst],
                           input=arr.astype(np.float32).tobytes(), capture_output=True)
        if p.returncode != 0:
            log("⚠ 沉淀进配乐库失败（不影响本次出片）：" +
                p.stderr.decode("utf-8", "ignore")[-200:])
            return None
        idx = os.path.join(d, LIB_INDEX)
        items = []
        if os.path.isfile(idx):
            try:
                with open(idx, "r", encoding="utf-8") as f:
                    items = (json.load(f).get("items") or [])
            except Exception:
                items = []
        items.append({"file": name, "source": source,
                      "prompt": str(cfg.get("prompt") or "")[:200],
                      "seed": cfg.get("seed"), "seconds": round(len(arr) / float(sr), 2),
                      "created": datetime.now().strftime("%Y-%m-%d %H:%M")})
        with open(idx, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        log(f"已沉淀进配乐库：02_素材真值/配乐/{name}（下次可复用）")
        return dst
    except Exception as ex:
        log(f"⚠ 沉淀进配乐库失败（不影响本次出片）：{ex}")
        return None


# ══════════════════════════════════════════ 路 4：在线免费曲库
def _secret(name):
    """从仓库的 config/secrets.local.json 取一个 key。

    ★ 为什么不在 IP 目录里找：IP 目录是档案区（/cfu/ai视频档案库/...），
      跟仓库不是一棵树，往上走找不到 config/。所以先看 STUDIO_HOME
      （pipeline.py 会给子进程注入），再按本脚本位置往上找（跑引擎真源时用）。
      都找不到就返回空 —— 缺 key 会走下面那条「明说缺什么」的分支，不会静默。
    """
    for base in [os.environ.get("STUDIO_HOME") or ""] + [
            os.path.dirname(os.path.abspath(__file__))]:
        d = base
        for _ in range(6):
            if not d:
                break
            p = os.path.join(d, "config", "secrets.local.json")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        v = (json.load(f) or {}).get(name)
                    return str(v or "").strip()
                except Exception:
                    return ""
            d = os.path.dirname(d)
    return ""


def src_online(total, cfg, log):
    """Jamendo / Freesound 下载一首可商用的曲子。

    ★ 这两个是真有 API 的。注意别指望 Pexels —— 它的 API 只有图片和视频，
      **没有音乐端点**（实测 /v1/music 之类全是 404）。Pixabay 同理，
      网页上有音乐、API 里没有。
    """
    o = cfg.get("online") or {}
    provider = str(o.get("provider") or "").strip().lower()
    key = str(o.get("api_key") or os.environ.get("BGM_API_KEY") or "").strip()
    if not key and provider:
        # 没在 ip.json 里填，就去本地密钥文件拿（config/secrets.local.json，不进 git）。
        # 这样 key 不用写在档案卡的明文里，也不会被同步到别处。
        key = _secret(provider)
    query = str(o.get("query") or cfg.get("prompt") or "ambient").strip()
    if provider not in ("jamendo", "freesound"):
        raise RuntimeError("bgm.online.provider 目前只支持 jamendo / freesound")
    if not key:
        raise RuntimeError(
            f"走 {provider} 要有 key，现在没填（ip.json 的 bgm.online.api_key）。\n"
            f"    Jamendo 在 developer.jamendo.com 申请 client_id；\n"
            f"    Freesound 在 freesound.org/apiv2/apply 申请 token。\n"
            f"    都没有的话，用 --source synth 或 comfy，一样能出片。")

    if provider == "jamendo":
        import urllib.parse
        q = urllib.parse.urlencode({"client_id": key, "format": "json", "limit": 10,
                                    "search": query, "include": "musicinfo",
                                    "audioformat": "mp32"})
        api = "https://api.jamendo.com/v3.0/tracks/?" + q
    else:
        import urllib.parse
        q = urllib.parse.urlencode({"token": key, "query": query, "page_size": 10,
                                    "filter": "duration:[20.0 TO 600.0]", "fields": "id,name,previews"})
        api = "https://freesound.org/apiv2/search/text/?" + q

    with urllib.request.urlopen(api, timeout=45) as r:
        data = json.load(r)
    rows = data.get("results") or []
    if not rows:
        raise RuntimeError(f"{provider} 搜「{query}」没结果")
    row = rows[0]
    if provider == "jamendo":
        url = row.get("audio")
        name = row.get("name")
    else:
        url = (row.get("previews") or {}).get("preview-hq-mp3")
        name = row.get("name")
    if not url:
        raise RuntimeError(f"{provider} 这条没给下载链接（{name}）")

    log(f"在线曲库：{provider} 《{name}》")
    import urllib.parse
    tmp = "/tmp/_bgm_online_" + os.path.basename(urllib.parse.urlparse(url).path or "x.mp3")
    with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as f:
        f.write(r.read())
    return _decode_file(tmp)


# ══════════════════════════════════════════ 公共：解码 / 裁剪 / 落盘
def _decode_file(path):
    """任何格式 → float32 单声道 + 采样率。"""
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-i", path,
                        "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("解不出音频：" + p.stderr.decode("utf-8", "ignore")[-400:])
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64), SR


def _fit(a, sr, total, log):
    """裁 or 循环拼到精确时长，首尾淡入淡出。"""
    need = int(total * sr)
    if len(a) < need:
        reps = int(np.ceil(need / max(1, len(a))))
        a = np.tile(a, reps)
        log(f"曲子比片子短，循环了 {reps} 遍")
    a = a[:need]
    fi = int(0.7 * sr)
    fo = int(1.2 * sr)
    a = a.astype(np.float64)
    if len(a) > fi + fo:
        a[:fi] *= np.linspace(0, 1, fi)
        a[-fo:] *= np.linspace(1, 0, fo)
    peak = np.max(np.abs(a)) or 1.0
    return (a / peak * 0.92).astype(np.float32)


SOURCES = {"synth": src_synth, "comfy": src_comfy, "reuse": src_reuse,
           "library": src_library, "online": src_online}
# ★ 这三条路生成出来的东西值得沉淀（生成一次，之后反复用）。
#   reuse 自己不沉淀 —— 它本来就是从库里来的，再存一份是翻倍；
#   library 也不沉淀 —— 那是人自己在外面维护的曲库，复制进来两份真值会打架。
ARCHIVABLE = {"synth", "comfy", "online"}


def load_cfg():
    """真源：ip.json 的 bgm 段。没建过 IP 的场景退回默认值（比如引擎自测）。"""
    cfg = {"source": "synth", "seed": 20261027, "sampler": {}}
    try:
        # ★ strict=False：配乐是「有更好、没有也能出片」的参数。
        #   严格要求 bgm 段存在的话，老档案会因为没配过音乐而直接失败。
        from ip_guard import load_ip
        cfg.update(load_ip(strict=False, verbose=False).get("bgm") or {})
    except Exception:
        pass
    return cfg


def main():
    import argparse
    ap = argparse.ArgumentParser(description="配乐生成（四条路挑一条）")
    ap.add_argument("out")
    ap.add_argument("seconds", nargs="?", type=float, default=34.92)
    ap.add_argument("--source", default="")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--pick", default="")
    ap.add_argument("--random", action="store_true")
    ap.add_argument("--library-dir", default="")
    a = ap.parse_args()

    _  = []   # 占位：保留结构，避免误加未用变量

    def log(m):
        print("   " + m, flush=True)

    cfg = load_cfg()
    source = (a.source or os.environ.get("SOURCE") or cfg.get("source") or "synth").strip().lower()
    if source not in SOURCES:
        sys.exit(f"没这条路：[{source}]\n    可选：{' / '.join(SOURCES)}")
    if a.prompt:
        cfg["prompt"] = a.prompt
    if a.seed is not None:
        cfg["seed"] = a.seed
    if a.pick:
        cfg["pick"] = a.pick
    if a.random:
        cfg["random"] = True
    if a.library_dir:
        cfg["library_dir"] = a.library_dir

    total = float(a.seconds)
    print(f"配乐来源 [{source}]  目标 {total:.2f}s", flush=True)

    if os.environ.get("DRY"):
        print("  DRY=1：不真跑。将要做的 ——")
        print(f"    源     {source}")
        print(f"    提示词 {str(cfg.get('prompt') or '(无)')[:120]}")
        print(f"    种子   {cfg.get('seed')}")
        if source == "library":
            fs = library_files(cfg.get("library_dir") or "")
            print(f"    曲库   {cfg.get('library_dir') or '(没配)'}  找到 {len(fs)} 首")
            for f in fs[:8]:
                print("            -", os.path.basename(f))
        if source == "comfy":
            ok, why = comfy_available()
            print(f"    本机可用 {'是' if ok else '否 → ' + why}")
            print(f"    DiT   {M3_MODELS['dit']}")
            print(f"    CLIP  {M3_MODELS['clip']}")
            print(f"    VAE   {M3_MODELS['vae']}")
        if source == "reuse":
            d = _lib_dir()
            cs = reuse_candidates(d) if os.path.isdir(d) else []
            print(f"    配乐库 {d or '(没有 IP 目录)'}  共 {len(cs)} 首")
            for c in cs[:8]:
                tag = f"[{c['source']}]" if c["source"] else ""
                print(f"            - {c['name']} {tag} {c['seconds'] or '?'}s {c['created']}")
            if not cs:
                print("            （空的 —— 先跑一次 synth 或 comfy，那首会自动沉淀进来）")
        print(f"    输出   {a.out}")
        return

    try:
        raw, sr = SOURCES[source](total, cfg, log)
        arr = _fit(raw, sr, total, log)
    except Exception as ex:
        print(f"\n✗ [{source}] 这条路没走通：{ex}", file=sys.stderr)
        print("  换一条：`--source synth` 永远能用（几秒出结果、不占 GPU）", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    p = subprocess.run([FF, "-nostdin", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                        "-ar", "48000", "-ac", "2", "-b:a", "192k", a.out],
                       input=arr.tobytes(), capture_output=True)
    if p.returncode != 0:
        print(p.stderr.decode("utf-8", "ignore")[-800:])
        sys.exit(1)
    # ★ 生成完了顺手沉淀一份 —— 下次 reuse 就有得挑，不用再生成一遍。
    if source in ARCHIVABLE:
        archive(arr, sr, source, cfg, log)
    print("OK", a.out, f"{total:.2f}s", flush=True)


if __name__ == "__main__":
    main()
