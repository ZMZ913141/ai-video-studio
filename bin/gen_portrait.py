#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接新客户第一步：生成形象底图 / 起始静帧（Qwen-Image 2.1，纯文生图）

为什么要有这个脚本
==================
新客户的档案里，`visual.portrait` 和 `visual.still` 一开始是空的。
以前的做法是让人去 ComfyUI 界面手搓一套工作流、再手动把图拷进档案目录 ——
这一步既不进流水线、也不留痕，换个人就复现不出来。

现在：一条命令，图直接落进本 IP 的 02_素材真值/，文件名按约定，
`ip.json` 里该填什么也会打印出来。人是「描述」这个角色，不是「操作工作流」。

两张图不是一回事（别混）
========================
  portrait 形象底图  1088×1920，胸口以上、留足头顶空间 —— 身份参考，用来做图生图/换背景
  still    起始静帧  864×1536（= 出片分辨率），视频模型就是拿它当第一帧
两张都用同一段描述出，但**分别生成**：把一张缩到另一个尺寸，构图会被切掉，
头顶空间不够时 H3 一动就顶到画框。

用法
====
  # 先看一眼要提交什么（不跑）
  python3 bin/gen_portrait.py --ip 演示客户老王 --who "五十多岁的面馆老板…" --dry

  # 出图（会占 GPU，约 1-2 分钟）
  python3 bin/gen_portrait.py --ip 演示客户老王 --who "…" --scene "…"

  # 只出其中一张
  python3 bin/gen_portrait.py ... --only portrait

★ 模型名是硬编码在下面 MODEL_* 里的，改了要跟 ComfyUI 里实际文件名核对 ——
  名字差一个字符，节点报的是「模型不可用」，看起来像没下载。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_OUT = os.environ.get("COMFY_OUT", "/cfu/ComfyUI/output")

MODEL_UNET = os.environ.get("MODEL", "qwen_image_2.1_turbo_int8_convrot.safetensors")
MODEL_CLIP = os.environ.get("CLIP", "qwen3vl_8b_int8_convrot.safetensors")
MODEL_VAE = os.environ.get("VAE", "qwen_image_2.1_vae_bf16.safetensors")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

NEG = ("cartoon, illustration, painting, 3d render, plastic skin, distorted face, "
       "extra fingers, deformed hands, blurry face, text, watermark, logo, "
       "harsh hard shadow, cutout edge, halo, pasted-on look, low resolution, "
       "oversaturated, HDR look, wig, mask")

# 两张图各自的取景要求。人物描述由用户给，这里只管「怎么取景」。
FRAMING = {
    "portrait": ("Chest-up portrait, vertical 9:16, the subject looks straight into the lens "
                 "with a calm friendly expression. Generous headroom above the hair, "
                 "shoulders visible at the bottom edge, centred composition."),
    "still": ("Chest-up talking-head framing for a vertical 9:16 video, the subject looks into "
              "the lens as if about to speak, mouth closed. A broad band of background above "
              "the top of the hair, chest and shoulders at the bottom edge."),
}
SIZE = {"portrait": (1088, 1920), "still": (864, 1536)}


def post(api, data):
    req = urllib.request.Request(COMFY + api, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:1200]))
        raise


def poll(pid, timeout=1800):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=30) as r:
                h = json.loads(r.read().decode())
        except Exception:
            time.sleep(3)
            continue
        if pid in h:
            return h[pid]
        time.sleep(3)
    raise SystemExit("ComfyUI 超时没出图（%s）" % pid)


def graph(prompt, w, h, seed, steps, cfg, ref=None):
    g = {
        "10": {"class_type": "UNETLoader", "inputs": {"unet_name": MODEL_UNET,
                                                      "weight_dtype": "default"}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODEL_CLIP,
                                                      "type": "qwen_image", "device": "default"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": MODEL_VAE}},
        "15": {"class_type": "EmptyLatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "16": {"class_type": "KSampler", "inputs": {
            "model": ["10", 0], "latent_image": ["15", 0], "seed": seed,
            "steps": steps, "cfg": cfg, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0}},
        "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["12", 0]}},
        "18": {"class_type": "SaveImage", "inputs": {"images": ["17", 0],
                                                     "filename_prefix": "portrait/gen"}},
    }
    enc = {"clip": ["11", 0], "prompt": prompt, "negative_prompt": NEG,
           "resolution": 0, "vae": ["12", 0]}
    if ref:
        # 图生图：拿已经定好的底图当身份参考，第二张才不会长成另一个人。
        # 两张图脸不一样，出片时看着像换了演员 —— 这是最容易被客户一眼看穿的错。
        enc["images.image_1"] = ["13", 0]
        g["13"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
    g["14"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    g["16"]["inputs"]["positive"] = ["14", 0]
    g["16"]["inputs"]["negative"] = ["14", 1]
    return g


def push_input(path):
    """LoadImage 只认 ComfyUI 的 input 目录，先拷进去（同名覆盖）。"""
    d = os.environ.get("COMFY_IN", "/cfu/ComfyUI/input/studio")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(path))
    shutil.copy2(path, dst)
    return "studio/" + os.path.basename(path)


def grab(hist, dst):
    """把 ComfyUI output 里的图拷到档案目录（不覆盖：同名自动加序号）。"""
    outs = []
    for node in (hist.get("outputs") or {}).values():
        for im in (node.get("images") or []):
            outs.append(os.path.join(COMFY_OUT, im.get("subfolder") or "", im["filename"]))
    if not outs:
        raise SystemExit("任务跑完了但没有图输出：%s" % json.dumps(hist)[:400])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # 旧图留档，别悄悄被覆盖；但 0 字节的占位文件直接盖掉 —— 留它只会添乱
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        stem, ext = os.path.splitext(dst)
        dst = "%s_旧%s%s" % (stem, time.strftime("%m%d%H%M"), ext)
    shutil.copy2(outs[0], dst)
    return dst, outs[0]


def probe(path):
    r = subprocess.run(["/usr/bin/file", path], capture_output=True, text=True)
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description="生成形象底图 / 起始静帧（Qwen-Image 2.1）")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--who", required=True, help="这个人是谁、长什么样、穿什么（英文更好使）")
    ap.add_argument("--scene", default="", help="背景/环境描述")
    ap.add_argument("--only", choices=["portrait", "still", "both"], default="both")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out-dir", default="", help="默认写进该 IP 的 02_素材真值/")
    ap.add_argument("--from", dest="ref", default="",
                    help="拿这张图当身份参考（图生图）—— 出第二张时用它，脸才一致")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    from vstudio import resolve_ip
    e = resolve_ip(a.ip)
    out_dir = a.out_dir or os.path.join(e["path"], "02_素材真值")
    prefix = e["name"].replace("演示客户", "") or "形象"

    jobs = []
    for kind in (["portrait", "still"] if a.only == "both" else [a.only]):
        w, h = SIZE[kind]
        prompt = " ".join(x for x in (
            "Photorealistic photo of", a.who.strip().rstrip("。."),
            ("Set in " + a.scene.strip().rstrip("。.") + ".") if a.scene.strip() else "",
            FRAMING[kind],
            "Real photography, natural soft light, shallow depth of field, sharp on the face, "
            "vertical 9:16.",) if x)
        ext = "形象底图" if kind == "portrait" else "起始静帧"
        dst = os.path.join(out_dir, "%s_%s_%dx%d.png" % (prefix, ext, w, h))
        jobs.append((kind, prompt, w, h, dst))

    for kind, prompt, w, h, dst in jobs:
        print("\n── %s  %dx%d\n   → %s\n   %s" % (kind, w, h, dst, prompt))
    if a.dry:
        print("\n（--dry：只打印，不提交）")
        return 0

    # 出图前先看内存：ComfyUI 用 --high-ram 换速度，长跑后内存会累积
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=10) as r:
            st = json.loads(r.read().decode())
        free_gb = st["system"]["ram_free"] / 2 ** 30
        print("\n内存可用 %.1f GB" % free_gb)
        if free_gb < 8:
            print("⚠ 可用内存不足 8GB，先跑 comfy_free.py 卸载模型再提交", file=sys.stderr)
    except Exception as ex:
        raise SystemExit("连不上 ComfyUI（%s）：%s" % (COMFY, ex))

    for i, (kind, prompt, w, h, dst) in enumerate(jobs):
        seed = (a.seed or int(time.time())) + i * 7
        ref = push_input(a.ref) if a.ref else None
        if ref:
            print("   身份参考：%s" % a.ref)
        t0 = time.time()
        pid = post("/prompt", {"prompt": graph(prompt, w, h, seed, a.steps, a.cfg, ref)})["prompt_id"]
        hist = poll(pid)
        dst, src = grab(hist, dst)
        print("✓ %s → %s  (%.0fs, seed=%s)" % (kind, dst, time.time() - t0, seed))
        print("   %s" % probe(dst))
    print("\n下一步：把这两个路径填进 ip.json 的 visual.portrait / visual.still")
    return 0


if __name__ == "__main__":
    sys.exit(main())
