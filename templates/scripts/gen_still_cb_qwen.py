#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈洁斌主号 · 静帧 C 方案（轻奢会客厅）· Qwen-Image-2.1 图生图
底图 = 档案白底形象底图（cb_base.png），只换背景，人物锁定
链路：UNETLoader(qwen_image_2.1_int8) + CLIPLoader(pe_i2i, qwen_image) + TextEncodeQwenImage21(带参考图) + KSampler
用法：python gen_still_cb_qwen.py            # 默认 C 方案
      PLAN=A|B|C python gen_still_cb_qwen.py  # 切方案
      STEPS=8 CFG=2.5 python gen_still_cb_qwen.py
"""
import json, os, sys, time, shutil, subprocess, urllib.request, urllib.error

COMFY = "http://127.0.0.1:8188"
COMFY_OUT = "/cfu/ComfyUI/output"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

SEED = int(os.environ.get("SEED", "881204201"))
STEPS = int(os.environ.get("STEPS", "8"))
CFG = float(os.environ.get("CFG", "1.0"))
PLAN = os.environ.get("PLAN", "C").upper()
MODEL = os.environ.get("MODEL", "qwen_image_2.1_turbo_int8_convrot.safetensors")

# T8 编辑契约 v1：开头即操作 → 明确改动区域 → 其余指原图保留；
# 面部/服装/构图只"指原图"，严禁长清单重新发明（re-inventory 会把编辑带偏）
BG = {
    "C": ("Change the plain white background of this photo into a quiet luxury lounge: a soft beige plaster wall, "
          "the blurred silhouette of a large green plant on the right, a warm floor lamp glowing softly at the left "
          "edge, shallow depth of field, calm and minimal."),
    "A": ("Change the plain white background of this photo into a modern clothing boutique: blurred wooden racks with "
          "earth-tone and dark garments, warm spotlights forming soft bokeh, shallow depth of field."),
    "B": ("Change the plain white background of this photo into a dark AI digital studio: deep navy-black tones, "
          "vertical screen light bars in soft blue-white, faint light particles, a faint floor reflection, shallow "
          "depth of field."),
}

PROMPT = (BG[PLAN] + " Preserve the man, his rimless rectangular glasses, his black brushed-up hair, his cream-white "
          "mandarin-collar shirt with frog closures, his pose and the original chest-up framing exactly as in the "
          "original image. Real photography quality, vertical 9:16.")

NEG = ("cartoon, illustration, painting, 3d render, plastic skin, distorted face, changed face, different person, "
       "extra fingers, deformed hands, blurry face, text, watermark, logo, harsh hard shadow, white background, "
       "cutout edge, halo, pasted-on look, low resolution")


def build_graph():
    return {
        "10": {"class_type": "UNETLoader", "inputs": {
            "unet_name": MODEL, "weight_dtype": "default"}},
        "11": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": os.environ.get("CLIP", "qwen3vl_8b_int8_convrot.safetensors"),
            "type": "qwen_image", "device": "default"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "13": {"class_type": "LoadImage", "inputs": {"image": "minimax_h3_timeline_director/cb_base.png"}},
        "14": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["11", 0], "prompt": PROMPT, "negative_prompt": NEG,
            "resolution": 0, "vae": ["12", 0], "images.image_1": ["13", 0]}},
        "15": {"class_type": "KSampler", "inputs": {
            "model": ["10", 0], "positive": ["14", 0], "negative": ["14", 1], "latent_image": ["14", 2],
            "seed": SEED, "steps": STEPS, "cfg": CFG, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0}},
        "16": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["12", 0]}},
        "17": {"class_type": "SaveImage", "inputs": {
            "images": ["16", 0], "filename_prefix": "still/cb_plan%s" % PLAN}},
    }


def post(api, data):
    req = urllib.request.Request(COMFY + api, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:1500])); raise


def poll(pid, timeout=3600):
    """等出图结果。服务挂了要报错，别死等（同 gen_tts / gen_video 的坑）。"""
    dl = time.time() + timeout
    bad = 0
    while time.time() < dl:
        try:
            with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=30) as r:
                h = json.loads(r.read().decode())
            bad = 0
        except Exception as ex:
            bad += 1
            if bad >= 8:                # 约 64 秒连不上 = 服务没了
                sys.exit(f"✗ 连不上 ComfyUI（{COMFY}）：{type(ex).__name__}: {ex}\n"
                         f"  任务 {pid} 拿不到结果，已停止等待。\n"
                         f"  先确认：curl -s --noproxy '*' {COMFY}/system_stats")
            time.sleep(8); continue
        if pid in h:
            return h[pid]
        time.sleep(8)
    return None


def free():
    subprocess.run([sys.executable, os.path.join(OUT_DIR, "comfy_free.py")],
                   capture_output=True, text=True)


if os.environ.get("DRY"):
    print("[DRY] plan=%s steps=%d cfg=%s seed=%d 提示词 %d 字" % (PLAN, STEPS, CFG, SEED, len(PROMPT)))
    print(PROMPT)
    sys.exit(0)

free()  # 跑前先清场，避免与 TTS/H3 模型叠加爆显存
res = post("/prompt", {"prompt": build_graph(), "client_id": "c_%d" % int(time.time())})
print("submitted", res["prompt_id"], flush=True)
r = poll(res["prompt_id"])
if not r or r.get("status", {}).get("status_str") != "success":
    print("失败:", json.dumps((r or {}).get("status", {}), ensure_ascii=False)[:1000]); free(); sys.exit(1)

imgs = []
for _, out in (r.get("outputs") or {}).items():
    for im in out.get("images", []):
        imgs.append(os.path.join(COMFY_OUT, im.get("subfolder", ""), im["filename"]))
if not imgs:
    print("无图片输出:", json.dumps(r.get("outputs", {}), ensure_ascii=False)[:500]); free(); sys.exit(1)
src = max(imgs, key=os.path.getmtime)
dst = os.path.join(OUT_DIR, "陈洁斌_主号_静帧_%s方案_%d.png" % (PLAN, SEED))
shutil.copy2(src, dst)
print("OK ->", dst, os.path.getsize(dst))

free()  # 跑完立即释放显存
