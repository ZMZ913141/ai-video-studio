#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 H3 正片出片：SRT → 自动分段 → 一次出片

效率提升点（相比 S01/S02 的一次性脚本）
====================================
1. **台词时间轴从 SRT 自动取**，不再手工维护 LINES 表。
   S01/S02 都要人肉对照 silencedetect 校准每句起点，这里直接读 SRT 的 t0。
2. **分段自动计算**：给定目标段数，自动选切点，并强制
   `段长 ≡5 (mod 17)` / `重叠 ≡5 (mod 17)` / `重叠 < 较短段长`。
3. 生成的提示词结构与人设圣经定板一致，不重新发明。

核心机制（2026-10-27 定论，别改回独立段模式）
==========================================
`segmentConfig.count=N` 原生多窗口：采样器内部用 previous_latent 把上一段
采样出的 latent 尾部真的接进下一段开头，Finalize 裁掉重叠帧自动拼接。
→ 不需要 ffmpeg 拼接、不需要交叉淡化，接缝数学上不存在。

用法:
  DRY=1          打印分段表与每段提示词（并校验约束）
  PILOT=1        只出第 1 段（验证提示词与身份）
  PILOT=last     只出最后一段（★ 验收「后段变油腻」必须用这个）
  PILOT=3        只出第 3 段
  直接跑         出全片
  SEG_GRID       ★ 最优写法：直接给每段「17 格数」，切点一步算出来（2026-10-27）
                 恒等式：段长 L=5+17n、重叠 5 ⇒ 切点 c_i = ΣL−5i = 17×Σn
                 → **切点必然落在 17 的整数倍上，不需要任何搜索/吸附**
                 语法：`9,10,7,7,11,9`（六段格数）或 `9,10,7,7,11,?`（末段自动补齐）
                 总格数约束：净长 = 17×Σn + 5（S03 906 帧 ⇒ Σn=53）
                 反向用法：想让切点落在某秒 → ×24 得帧 → ÷17 取最近整数 → 相邻相减
                 ⚠️ 但卡点上是不是停顿是另一回事：格数要照停顿窗口反算，不能均分
  SEG_OVERRIDE   手动指定分段，不做等长均分（2026-10-27 实现）
                 两种写法：`0-192,187-379,374-549,544-906`（起点-终点）
                           `0,187,374,544`（只写起点，终点顺推）
                 每段时长可以随意不等，只要满足采样器硬约束：
                 首段从 0 起、`L≡5(mod17)`、`重叠≡5(mod17)`、
                 重叠 < 较短段长、逐段严格前推、末段到 total
                 例：想让第 2 段多说一句就写 0-192,187-413,408-906
  OVERLAP=22     临时改重叠帧（默认 5），用于 A/B
  FAST=1         快速测试档 MP=0.5（544x960）。只验提示词/分段/动作/口型，
                 ❌ 不能验清晰度、曝光、面部细节（那些量本身就是分辨率的函数）

  例: SRT=../07_成片/S03_同行都在拍/S03_同行都在拍.srt \
      AUDIO=../06_音频/S03_同行都在拍/S03_音频_定板_37.2s.mp3 \
      TAG=S03 gen_video.py
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = "http://127.0.0.1:8188"
COMFY_OUT = "/cfu/ComfyUI/output"
IN_DIR = "/cfu/ComfyUI/input/minimax_h3_timeline_director"
REL = "minimax_h3_timeline_director"
HERE = os.path.dirname(os.path.abspath(__file__))
FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

TAG = os.environ.get("TAG", "S03")
SRT = os.environ.get("SRT", "")
AUDIO_SRC = os.environ.get("AUDIO", "")
# ★★ 多 IP 隔离：静帧决定「这是谁的脸」，真值来自本 IP 的 ip.json
#   2026-10-28 改。旧写法两个致命问题：
#     ① 靠 glob 从 ../02_素材真值/*静帧*.png 取，在错的目录跑就指向错的 IP
#     ② 兜底硬编码「陈洁斌_主号_静帧_...」→ 忘了设 STILL 就直接长出洁斌的脸
#   现在改为按脚本路径锁定 IP，素材缺失直接 sys.exit，绝不静默退回别人的素材。
#   环境变量 STILL 仍可用，但指向本 IP 之外会被 ip_guard 拒绝。
# 注意：IP 档案的加载放在 UNET/CLIP 常量定义之后（见文件下方），
# 因为档案里的值是它们的**默认值来源**，顺序反了会 NameError。
AUDIO_NAME = f"{TAG}_audio.mp3"
STILL_NAME = f"{TAG}_still.png"

FPS = 24
SEED = int(os.environ.get("SEED", "881208001"))
# PILOT：不设置=全片；"1"=第1段；"last"=最后一段；"3"=第3段
PILOT_RAW = (os.environ.get("PILOT") or "").strip().lower()
PILOT = bool(PILOT_RAW) and PILOT_RAW not in ("0", "false", "off")
PILOT_SPEC = PILOT_RAW if PILOT else ""
# ★ 名字曾经骗人：这串数字是「目标**段数**」，不是「每段多少秒」。
#   2026-10-30 就有人（我）照字面传了 TARGET_SECS=8 想“每段 8 秒”，结果出了 8 段。
#   新名 TARGET_SEGS 优先，老名 TARGET_SECS 继续兼容（外部脚本/旧命令不要立刻炸）。
TARGET_SEGS = int(os.environ.get("TARGET_SEGS") or os.environ.get("TARGET_SECS") or "5")
if os.environ.get("TARGET_SECS") and not os.environ.get("TARGET_SEGS"):
    print("  ℹ TARGET_SECS 是旧名，含义是「目标段数」不是秒数；新名 TARGET_SEGS（本片按段数 "
          f"{TARGET_SEGS} 走）", file=sys.stderr)

# ★ 2026-10-28 晚按用户截图定板：ref2va 底座 + DMD 4step 蒸馏 LoRA + Sage 注意力
#   换掉原来的 fused_refdelta_turbo8 + nvfp4 CLIP。三点必须一起改，缺一个就白跑：
#     ① UNet 从 turbo8 换成 ref2va_pruned（配套 DMD LoRA 的底座）
#     ② CLIP 换 int8_convrot（nvfp4 是给旧底座配的，跟 ref2va 不搭）
#     ③ LoRA 必须插在 UNETLoader 之后、**调度器和采样器共用的那个 model 上**
#   ⚠ LoRA 实际文件名是 dmad_ 不是 dmd_（实测目录），差一个字母节点判不可用，
#     但报错只说「lora not found」，看起来像没下载。
UNET = "Minimax-h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
LORA = "minimax_h3/dmad_minimax_h3_4step_full_critic_comfyui.safetensors"
LORA_STRENGTH = 1.0
SAGE = "auto"          # PathchSageAttentionKJ 的 sage_attention；"disabled" = 不挂
STEPS = 8              # 截图定板 8 步（DMD 是 4step 蒸馏，但实测 8 步也走这条线）
UPSCALER = "minimax_h3_latent_upscaler_3d_fp16_V2.safetensors"

# ★★ 多 IP 隔离：静帧决定「这是谁的脸」，真值来自本 IP 的 ip.json
#   2026-10-28 改。旧写法两个致命问题：
#     ① 靠 glob 从 ../02_素材真值/*静帧*.png 取，在错的目录跑就指向错的 IP
#     ② 兜底硬编码「陈洁斌_主号_静帧_...」→ 忘了设 STILL 就直接长出洁斌的脸
#   现在改为按**脚本路径**锁定 IP，素材缺失直接 sys.exit，
#   **绝不静默退回别人的素材**。环境变量 STILL 仍可用，
#   但指向本 IP 之外会被 ip_guard 拒绝。
from ip_guard import load_ip

IP = load_ip(verbose=False)
STILL_SRC = IP.asset("visual", "still")
assert STILL_SRC and os.path.exists(STILL_SRC), f"静帧缺失：{STILL_SRC}"
UNET = IP.get("video.unet") or UNET
CLIP = IP.get("video.clip") or CLIP
UPSCALER = IP.get("video.second_pass.model") or UPSCALER
# 二采的两个开关以前是**写了不读**：ip.json 里 enabled / high_steps 齐备，
# 代码里却 `"secondPass": True, "secondPassHighSteps": 2` 硬写死。
# 后果是「想关掉二采省时间」关不掉、「想调高步数」调不动，而 ip.json 看着
# 什么都支持 —— 2026-10-30 排查时抓到的典型假开关。
# 插件那边确实认这两个值（bool(timeline["secondPass"])），接上就生效。
SECOND_PASS = IP.get("video.second_pass.enabled")
SECOND_PASS = True if SECOND_PASS is None else bool(SECOND_PASS)
SECOND_PASS_STEPS = int(IP.get("video.second_pass.high_steps") or 2)
# 模型链的另外三段也走档案卡：LoRA / Sage / 步数。IP 之间只差这三行，
# 换工作流（换人换背景那类）时也是改这里，引擎侧不用动。
LORA = IP.get("video.lora.name") or LORA
LORA_STRENGTH = float(IP.get("video.lora.strength") or LORA_STRENGTH)
SAGE = IP.get("video.sage_attention") or SAGE
STEPS = int(IP.get("video.steps") or STEPS)

# ══════════════════════════════════════════════════════ 分段素材（2026-10-30）
# 「客户提供新素材要怎么引用」「不同分段不同素材」——H3 原生就支持，只是我们没用。
# 规范不是猜的，是从本机插件源码读出来的，完整版见 docs/H3时间轴与分段素材规范.md。
# 三条最重要的：
#   ① 段的 images 是 **id 列表**，插件按段筛选后 <Picture N> **重新从 1 编号**
#      → 英文骨架里写死的 <Picture 1> 在第 2 段自动指到新素材，模板一个字不用改；
#   ② 段的 images **为空 = 这一段没有参考图**（不是"用全部图"）→ 会退化成文生视频、
#      脸直接崩。所以下面给每段都兜底成 ["img"]，绝不留空；
#   ③ 图上限 9 张，超了被静默截断、不报错。
MATERIALS = (os.environ.get("MATERIALS") or "").strip()
BASE_IMAGE_ID = "img"          # 起始静帧占用的 id，materials.json 不能再叫这个名
EXTRA_IMAGES = []              # [{"id","src","ext","name","w","h","note","in_name"}]
SEG_IMAGES = {}                # {段号(1起): [id, ...]}
DEFAULT_IMAGE_IDS = [BASE_IMAGE_ID]


def _image_size(path, fallback=(1088, 1920)):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return fallback


def _load_materials():
    """读 materials.json。没有这个文件 → 行为跟加这段之前一字不差。"""
    if not MATERIALS or not os.path.isfile(MATERIALS):
        return [], {}, [BASE_IMAGE_ID], [], {}

    try:
        with open(MATERIALS, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as ex:
        sys.exit(f"✗ materials.json 读不了：{MATERIALS}\n  → {ex}")

    root = os.path.abspath(IP.root)
    extras = []
    for i, it in enumerate(doc.get("images") or []):
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").strip()
        rel = str(it.get("file") or "").strip()
        if not iid or not rel:
            sys.exit(f"✗ materials.json 第 {i+1} 张图缺 id 或 file")
        if iid == BASE_IMAGE_ID:
            sys.exit(f"✗ materials.json 里的 id 不能叫「{BASE_IMAGE_ID}」"
                     f"（那是起始静帧占用的名字）")
        src = IP.abspath(rel)
        # ★ 越界必须硬拦：跨 IP 引用素材 = 把上一位客户的脸/门店串到下一位身上。
        if not os.path.abspath(src).startswith(root + os.sep):
            sys.exit(f"✗ 素材不在本 IP（{IP.get('ip', '?')}）目录内：{rel}\n"
                     f"  禁止跨 IP 引用素材 —— 会把别人的脸/场景串进来。")
        if not os.path.isfile(src):
            sys.exit(f"✗ 素材文件不存在：{src}")
        w, h = _image_size(src)
        ext = os.path.splitext(src)[1].lower() or ".png"
        extras.append({"id": iid, "src": src, "name": os.path.basename(src),
                       "ext": ext, "in_name": f"{TAG}_mat_{iid}{ext}",
                       "w": w, "h": h, "note": str(it.get("note") or "")})

    known = {BASE_IMAGE_ID} | {m["id"] for m in extras}
    segmap = {}
    for k, v in (doc.get("segments") or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue                      # "default" / "*" 走下面那条分支
        ids = [str(x).strip() for x in (v if isinstance(v, list) else [v])]
        ids = [x for x in ids if x]
        if ids:
            segmap[idx] = ids

    default_ids = None
    for key in ("default", "*"):
        v = (doc.get("segments") or {}).get(key)
        if isinstance(v, list) and v:
            default_ids = [str(x).strip() for x in v if str(x).strip()]
            break
    if not default_ids:
        default_ids = [BASE_IMAGE_ID]

    for owner, ids in list(segmap.items()) + [("default", default_ids)]:
        bad = [x for x in ids if x not in known]
        if bad:
            sys.exit(f"✗ materials.json 引用了不存在的素材 id {bad}"
                     f"（{owner}）。已登记的：{sorted(known)}")
        if len(ids) > 9:
            sys.exit(f"✗ materials.json 里 {owner} 挂了 {len(ids)} 张图，H3 上限 9 张"
                     f"（超出的会被静默丢弃，不如现在就说清楚）")

    # ---- 参考视频 ----
    # 三种用途（插件 README 里写的，源码 _reference_mode 认这三个值）：
    #   guide    固定 Guide：重叠区按生成时间硬锁，适合**续写 / 保持运动**
    #   edit     可编辑参考：只作 <Video N> 参考，不硬锁原人物 → 换人 / 换服装 / 换风格
    #   boundary 仅固定边界：只锁重叠区首尾帧
    VID_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
    vids = []
    for i, it in enumerate(doc.get("videos") or []):
        if not isinstance(it, dict):
            continue
        vid = str(it.get("id") or "").strip()
        rel = str(it.get("file") or "").strip()
        if not vid or not rel:
            sys.exit(f"✗ materials.json 第 {i+1} 个视频缺 id 或 file")
        src = IP.abspath(rel)
        if not os.path.abspath(src).startswith(root + os.sep):
            sys.exit(f"✗ 参考视频不在本 IP 目录内：{rel}（禁止跨 IP 引用）")
        if not os.path.isfile(src):
            sys.exit(f"✗ 参考视频不存在：{src}")
        ext = os.path.splitext(src)[1].lower()
        if ext not in VID_EXT:
            sys.exit(f"✗ 参考视频格式不支持：{src}（要 {'/'.join(sorted(VID_EXT))}）")
        use = str(it.get("use") or "edit").strip().lower()
        if use not in ("guide", "edit", "boundary"):
            sys.exit(f"✗ 参考视频 {vid} 的 use={use!r} 不认识"
                     f"（只能 guide / edit / boundary）")
        vids.append({"id": vid, "src": src, "name": os.path.basename(src),
                     "in_name": f"{TAG}_vid_{vid}{ext}",
                     "use": use,
                     "trim_start": float(it.get("trimStart") or 0),
                     "note": str(it.get("note") or "")})

    vknown = {v["id"] for v in vids}
    vsegmap = {}
    for k, v in (doc.get("seg_videos") or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        ids = [str(x).strip() for x in (v if isinstance(v, list) else [v])]
        ids = [x for x in ids if x]
        if ids:
            bad = [x for x in ids if x not in vknown]
            if bad:
                sys.exit(f"✗ materials.json 的 seg_videos 引用了不存在的视频 id {bad}"
                         f"（已登记的：{sorted(vknown)}）")
            vsegmap[idx] = ids

    return extras, segmap, default_ids, vids, vsegmap


EXTRA_IMAGES, SEG_IMAGES, DEFAULT_IMAGE_IDS, EXTRA_VIDEOS, SEG_VIDEOS = _load_materials()
VIDEO_BY_ID = {v["id"]: v for v in EXTRA_VIDEOS}


def seg_image_ids(i_one_based):
    """第 i 段（1 起）用哪几张图。★ 永不返回空列表 —— 空 = 无参考图 = 脸崩。"""
    return SEG_IMAGES.get(i_one_based) or DEFAULT_IMAGE_IDS


# ★ 模型名自动补 .safetensors（防档案卡漏写后缀）
#   2026-10-28 踩的坑：ip.json 里写的是 "minimax_h3_latent_upscaler_3d_fp16_V2"
#   （漏了 .safetensors），提交后 ComfyUI 报
#       "The selected two-stage latent upscaler is unavailable: ..."
#   这个报错**看起来像模型没装**，实际是名字差一个后缀 —— 于是我先去查文件在不在、
#   查 folder_paths 缓存、查扩展名过滤，白花了很久，问题是档案卡自己写错的。
#   节点是拿这个名字去比对 folder_paths 的列表，差一个字符就判不可用。
#   → 档案里漏写也能自动补上，并打印一行提醒（补了什么一眼可见）。
def _sfx(name):
    """模型名补 .safetensors。已经是别的扩展名的不动。"""
    if not name:
        return name
    if "." in os.path.basename(name):
        return name
    fixed = name + ".safetensors"
    print(f"  ⚠ 模型名缺扩展名，已自动补：{name} → {fixed}")
    print("    （档案卡里请直接写全名，见 ip.json 的 video.second_pass._model_note）")
    return fixed


UNET = _sfx(UNET)
CLIP = _sfx(CLIP)
UPSCALER = _sfx(UPSCALER)

# 出片分辨率（9:16 竖屏，multiple=32）
# ★★ 2026-10-27 定版：**864x1536（MP=1.26）**，取代原默认 768x1376。
#    选它的唯一理由：它是 1.0~1.33 区间里**唯一精确 9:16** 的档（0.5625 = 9/16）。
#      768x1376 → 0.5581（横向被压 0.79%）
#      832x1504 → 0.5532（横向被压 1.6%）
#      864x1536 → 0.5625 ✓ 精确
#      896x1568 → 0.5714（横向被拉 1.6%）
#    后果：864x1536 乘 1.25 正好是 **1080x1920**（发布标准），
#    放大是纯整数倍、不重采样比例，不会二次畸变；其它档放大都要凑非整数。
#    成本：面积 +25.6%，实测耗时 +27%（**不是倍数**，见下方「耗时」说明）。
#   MP=1.0  → 768x1376   （S01/S02/S03 定版尺寸，仍可 `MP=1.0` 回退）
#   MP=1.26 → 864x1536   （★ 当前定版）
#   MP=1.33 → 896x1568   （S03 v5 用过；后段一致性 corr 掉到 −0.156，不推荐）
#   MP=2.0  → 1088x1920  （= 静帧原生尺寸；MAXLEN 只剩 90，接缝自由度归零，不推荐）
# ⚠️ 改了这个值，验收工具里的 `W, H = 768, 1376` 必须同步改，
#    否则 face_box / top_of 的坐标全错，量出来的 p99 是假数据。
#    （04_验收工具 里已全部走 probe_size.py 自适应，正常不用手改）
#
# ★ 关于「提高分辨率会不会倍数耗时」——不会，实测是**线性**：
#   显存占用 ∝ 面积 × 段长，而段长上限 MAXLEN 会随面积**自动按反比收紧**，
#   于是 面积↑ 与 段长↓ 相乘后总计算量基本持平，只多出「段数变多」带来的
#   接缝开销（每多一段多一次 warmup + 5 帧重叠）。
#   S03 实测：1.0MP/6段 = 1185s（19.75min）；1.26MP/8段 按面积×总帧外推 ≈ +27%。
#   真正的代价不是时间，是**段数从 6 涨到 8、接缝从 5 处涨到 7 处**。
MP = float(os.environ.get("MP", "1.26"))
MULTIPLE = 32
_BASE_MP = 1.008          # 768x1376 的实际 MP —— 这是「安全线标定基准」，不是当前出片档！
                          # ⚠️ 出片默认档已改为 1.26（864x1536），但 _BASE_MP/​_BASE_MAXLEN
                          # 仍是 **1.0MP/192帧** 这组实测数据。它们是「面积×帧数」这把尺子的刻度原点，
                          # 换掉等于把整把尺子换掉。**别跟着 MP 默认值一起改。**

# ---- 快速测试档（2026-10-27 用户要求）
# 「如果跑样片或测试，跟分辨率没关系的话，就跑低分辨率，又快又能得到答案。」
# FAST=1 → MP=0.5（544x960），面积约为 1.0MP 的 **49%**（≈减半，不是 1/4）。
#   纯功能验证更推荐 MP=0.25（384x672，24% 面积）。六档全部实测可用：
#       0.25→384x672  0.3→416x736  0.4→480x864
#       0.5 →544x960  0.6→608x1056 0.75→672x1184
#   适用：验提示词、验分段、验动作、验口型、验接缝位置、验时长、
#         验提示词有没有否定词、验出片有没有崩
#   ❌ 不适用：验清晰度、验曝光（>230%/p99）、验面部细节、验皮肤质感、验高光成斑
#      —— 这些量本身就是分辨率的函数，低分辨率得到的数**全是假的**。
#      判据很简单：验收指标里带像素/阈值/清晰度的，就必须用 MP=1.0 以上。
# 产物会带 `_FAST` 标记，防止低分辨率样片被当成成品交付。
if os.environ.get("FAST", "").strip().lower() in ("1", "true", "yes"):
    MP = float(os.environ.get("MP", "0.5"))
FAST = MP < _BASE_MP * 0.9      # <~0.9MP 一律视为快速档

# ★ 刻意让低分辨率**不放开** MAXLEN（`_maxlen_for` 在 mp≤1.008×1.02 时恒返回 192）。
#   实测 0.25 / 0.5 / 1.0 三档的**分段表逐帧完全相同**（158/175/124/124/192/158，
#   切点 153-323-442-561-748，5/5 落停顿）。所以「低分辨率验过的分段」对正式片是
#   **严格等价**而非近似 —— 这是 FAST 档最大的价值。一旦放开 MAXLEN，段数变少、
#   切点全变，验证结论立刻作废。除非只想看画面，否则**不要**为低分辨率放宽段长上限。

# ---------------------------------------------------------------- 低显存分块
# 2026-10-27 实测：1.33MP(896x1568) 在 16GB 卡上直接 OOM
#   （已分配 12.61GiB + 申请 1.01GiB > 15.57GiB 上限）。
# 用 WAS Suite 的 `WASH3LowVRAM`（专为 MiniMax H3 写的分块补丁）可以
# **不降分辨率**把 attention 的显存峰值压下来：
#   head_chunks=8  → attention 只跑 7 个 head（共 56 个），省约 1/3
#   query_chunks=4 → 按 token 分 4 块跑，省约 1/2
#   token_slice=4096 → norm/proj/ff 的工作内存再减半
# 节点 tooltip 明确写「Same result / The result matches up to float rounding」，
# 即分块不改变输出，只是把同一份注意力拆着算。
# LOWVRAM 环境变量：
#   0        不挂补丁（1.0MP 默认，速度最快）
#   1        中等分块（head 8 / query 4 / token 8192）
#   aggressive 强分块（head 8 / query 8 / token 4096）—— 1.33MP+ 推荐
LOWVRAM = os.environ.get("LOWVRAM", "").strip().lower()


def lowvram_settings():
    """返回 (head_chunks, query_chunks, token_slice)；全 0 表示不挂节点。"""
    if LOWVRAM in ("0", "", "off", "false"):
        # 高分辨率默认也要挂中等分块，否则一定 OOM
        if MP > _BASE_MP * 1.02:
            return (8, 4, 8192)
        return (0, 0, 8192)
    if LOWVRAM in ("aggressive", "2", "max"):
        return (8, 8, 4096)
    return (8, 4, 8192)


def calc_res(mp=MP, multiple=MULTIPLE):
    """复刻 ResolutionSelector 的算法，供脚本内部预知输出尺寸（写日志/校验用）。"""
    import math
    tp = mp * 1024 * 1024
    scale = math.sqrt(tp / (9 * 16))
    w = round(9 * scale / multiple) * multiple
    h = round(16 * scale / multiple) * multiple
    return w, h


# ---------------------------------------------------------------- 从 SRT 取时间轴

def parse_srt(path):
    txt = open(path, encoding="utf-8-sig").read()
    out = []
    for m in re.finditer(
            r"(\d+)\n(\d\d):(\d\d):(\d\d),(\d+) --> (\d\d):(\d\d):(\d\d),(\d+)\n(.*?)(?:\n\n|$)",
            txt, re.S):
        g = m.groups()
        t0 = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 1000
        t1 = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 1000
        out.append((t0, t1, g[9].strip().replace("\n", "")))
    return out


def audio_dur(path):
    """读音频时长（秒）。★ 2026-10-27 补错误提示 —— 原来文件不存在时
    正则匹配 None，报 `AttributeError: 'NoneType' object has no attribute 'group'`，
    完全看不出是「音频路径写错」。"""
    if not os.path.isfile(path):
        sys.exit(f"✗ 音频不存在：{path}\n"
                 f"  多 IP 场景请用 AUDIO=<本IP定板音频> 指定")
    p = subprocess.run([FF, "-nostdin", "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode(errors="replace"))
    if not m:
        sys.exit(f"✗ 读不出音频时长（不是有效音频？）：{path}\n"
                 f"  {p.stderr.decode(errors='replace')[:300]}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------------------------------------------------------------- 自动分段

# 段间重叠帧。硬约束：`OVERLAP ≡ 5 (mod 17)`（采样器 :458-465 会校验，
# `_valid_guide_frames` 只做「向下减到 %17==5」）。合法值 **5 / 22 / 39 / 56**。
# ⚠️ 2026-10-27 用户指定改用 5 帧。历史记录订正：
#   旧注释写「只能 22/39/56」，那是因为 `_valid_guide_frames(5)` 恰好等于 5，
#   条件 `(overlap and _valid(overlap) != overlap)` 不触发 → **5 一直是合法值**，
#   S01（34.9s）当初就是用的 5。之前那句「实测零接缝的唯一常用值」写错了。
#
# ★ 5 vs 22 的真实差别（不是「越小越好」，是取舍）：
#   采样器用 `_video_latent_t(n) = ((n-5)//17)*5 + 2` 把帧数换算成 latent 交接深度：
#       重叠  5 → latent T=2     重叠 22 → latent T=7
#   → 重叠 5 每个接缝只交接 2 帧 latent，连续性上下文**薄了 3.5 倍**；
#     但段数从 6 降到 5（接缝 5 处 → 4 处），且 `maxd = MAXLEN-OVERLAP`
#     从 170 放宽到 187，切点更容易落在句间停顿上。
#   → 要更少的接缝 + 更自由的切点，选 5；要更厚的接缝上下文，选 22。
#     用 `OVERLAP=22` 环境变量可随时回退做 A/B。
OVERLAP = int(os.environ.get("OVERLAP", "5"))
STEP = 17
# 段长上限：显存占用 ∝ 面积 × 帧数，所以提高分辨率必须同步缩短单段。
# 192 帧是 768x1376（1.0MP）在 16GB 卡上的实测安全线（约 12.6GiB 峰值）。
# 2026-10-27 实测：1.33MP(896x1568) 直接跑 175 帧 → torch.OutOfMemoryError
#   （已分配 12.61GiB + 申请 1.01GiB > 15.57GiB 上限）。
# 按面积反比折算：192 × (1.008/1.340) ≈ 144 帧，取 5+17n 的合法值 = 141。
# 这个值仍偏保守；真要更稳可以再降一档（124）。
# ★ 这两个常量是「面积×帧数」安全线的**刻度原点**，与当前出片档位无关。
#   现定版 1.26MP 算出 MAXLEN=141，峰值 1.266×141=178.5 < 193.5 安全线，余量 8%。
_BASE_MAXLEN = 192


def _maxlen_for(mp):
    """按目标分辨率算安全的单段帧数上限（5+17n 合法值）。

    ★★ 2026-10-27 修 bug：原来直接用**名义** mp 算，但 `calc_res()` 会把尺寸
    吸附到 32 的倍数，实际面积与名义 mp 并不相等。两者不一致时安全线会失守：

        名义 1.22 → budget=192×1.008/1.22=158.7 → MAXLEN=158
        实际尺寸 864x1504 = 1.2393MP
        → 峰值 ∝ 1.2393 × 158 = 195.8，**超过 192 的实测安全线**
        （1.0MP/192 帧就是 12.6GiB 那次 OOM 的临界点）

    这类错不会报错，只会等到渲染时 OOM。所以一律按**实际渲染尺寸**算。
    改完对已交付档位逐一核对，结果不变（1.0→192 / 1.10→175 / 1.19→158
    / 1.26→141 / 1.33→141），只有 1.22 这类「名义与实尺寸错位」的档会收紧。
    """
    w, h = calc_res(mp)
    real_mp = w * h / 1024 / 1024
    if real_mp <= _BASE_MP * 1.02:
        return _BASE_MAXLEN
    budget = _BASE_MAXLEN * (_BASE_MP / real_mp)     # 面积反比（用实测面积）
    n = int((budget - 5) // STEP)
    return 5 + STEP * max(1, n)


MAXLEN = _maxlen_for(MP)


def legal_len(L):
    """段长必须 L = 5 + 17n，且 <= MAXLEN（随分辨率自动收紧）。"""
    return L >= 5 and (L - 5) % 17 == 0 and L <= MAXLEN


def snap(L, lo=5, hi=MAXLEN):
    """把任意长度吸附到最近的合法段长（5+17n）。"""
    n = round((L - 5) / 17)
    L = 5 + 17 * max(0, n)
    return max(lo, min(hi, L))


def min_segs(total):
    """覆盖 total 帧最少要几段。n*MAXLEN-(n-1)*OVERLAP 是净长上限。"""
    n = 1
    while n * MAXLEN - (n - 1) * OVERLAP < total:
        n += 1
    return n


def slack_of(total, n):
    """★ 切成 n 段还剩多少**帧余量** —— 决定切点有没有活动空间。

    `ΣL = total + OVERLAP×(n−1)`（每个接缝多算一次重叠），上限 `MAXLEN×n`：
        slack = MAXLEN×n − (total + OVERLAP×(n−1))

    **判据：slack < 17×切点数 ⇒ 切点被锁死，排不出全落停顿的方案。**
    S03（906帧/192/重叠5）实测：
        n=5 → slack=34，只有 2 格要摊给 4 个切点
              ⇒ 枚举全部 15 种结构解，c1 只能 153/170/187（只有 153 是停顿）、
                c2 只有 374、c3 只有 561 ⇒ 最好 3/4，**没有一种能全落**
        n=6 → slack=221 ⇒ 5/5 全落 ✓
    这条比「能不能装下」实用：装得下但排不出好切点，等于白搭。
    """
    return MAXLEN * n - (total + OVERLAP * (n - 1))


# ---------------------------------------------------------------- 停顿检测
# ★ 为什么需要「能量法」而不只是 SRT（2026-10-27，借自 ai-jian-koubo 的思路）
# 停顿时段一直是从 SRT 的 cue 间隙反推的，这有两个漏洞：
#   1. `gap >= 0.20` 这个写死的阈值把短换气扔了。S03 有 1 个 160ms 的间隙
#      （10.24-10.40s，帧 246-250）直接被不计，而 160ms 听感上已经够切段。
#   2. **语义断句 ≠ 所有可切点**。cue 按标点切，句中自然的气口不体现为 cue 间隙。
# S03 实测：SRT 15 个窗口 / 16 个卡点，能量 24 段 / 23 个卡点，**互有独有**
#   能量独有 8 个 → 493 612 714 748 765 782 833 884
#   SRT  独有 1 个 → 136
#   ⇒ 候选池必须取**并集**（24 个），不是二选一。
# 可行域（带 MAXLEN 约束、6 段）：仅 SRT 11 种 → 并集 105 种，**9.5 倍**。
#   段数越多差距越大（7 段 14×、8 段 23×）。
#
# ⚠️ 本函数**不 import numpy** —— gen_video.py 必须在系统 python 下也能跑
#    （系统 python 没有 numpy）。纯 stdlib 实测 0.03s / 37s 音频，
#    与 numpy 版**逐段完全一致**（p90 与 24 个停顿时段全部相同），
#    差异仅 7.6e-6（float32 vs float64），不影响任何判定。

_PAUSE_CACHE = {}


def pause_windows(audio_path, min_sec=0.15, offset_db=20, force=False):
    """用真实音频能量找句间停顿，返回 [(起秒, 止秒), ...]。

    算法（与 04_验收工具/pause_energy.py 同源）：
      8kHz 单声道 → 每 10ms 算 RMS(dB) → max-pool ±2 帧（±20ms，填掉字内音素
      短瞬停）→ 人声线 = `p90 − offset_db` → 低于该线且持续 ≥min_sec 的段即停顿。

    ⚠️ 能量只说明「这里安静」，不说明「这里该切」。语义上是不是一句话的结尾
       仍要看 SRT 文本 → 所以本函数只**补漏**，不与 SRT 冲突、不用来替代它。
    """
    import array
    import math

    if not audio_path or not os.path.isfile(audio_path):
        return []
    key = (audio_path, min_sec, offset_db)
    if not force and key in _PAUSE_CACHE:
        return _PAUSE_CACHE[key]

    SR, FRAME_MS, POOL = 8000, 10, 2
    try:
        p = subprocess.run(
            [FF, "-nostdin", "-v", "quiet", "-i", audio_path, "-ac", "1", "-ar", str(SR),
             "-f", "s16le", "-"], capture_output=True)
        pcm = array.array("h")
        pcm.frombytes(p.stdout)
    except Exception:
        return []
    if not pcm:
        return []

    win = SR * FRAME_MS // 1000          # 每帧 80 个采样
    n = len(pcm) // win
    db = []
    for i in range(n):
        base = i * win
        s = 0.0
        for j in range(base, base + win):
            v = pcm[j]
            s += v * v
        rms = math.sqrt(s / win) / 32768.0
        db.append(20 * math.log10(rms) if rms > 1e-10 else -90.0)
    if not db:
        return []

    # max-pool：填掉字内音素短瞬停（<20ms），否则一个音节内部会被误判成静音
    env = [0.0] * len(db)
    for i in range(len(db)):
        env[i] = max(db[max(0, i - POOL):min(len(db), i + POOL + 1)])

    # p90：与 numpy.percentile(env, 90) 线性插值口径一致
    srt_env = sorted(env)
    pos = 0.9 * (len(srt_env) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(srt_env) - 1)
    p90 = srt_env[lo] + (srt_env[hi] - srt_env[lo]) * (pos - lo)
    thr = p90 - offset_db

    out, st = [], None
    for i, v in enumerate(env):
        q = v < thr
        if q and st is None:
            st = i
        elif not q and st is not None:
            if (i - st) * FRAME_MS / 1000 >= min_sec:
                out.append((st * FRAME_MS / 1000, i * FRAME_MS / 1000))
            st = None
    if st is not None and (len(env) - st) * FRAME_MS / 1000 >= min_sec:
        out.append((st * FRAME_MS / 1000, len(env) * FRAME_MS / 1000))

    _PAUSE_CACHE[key] = out
    return out


def pause_quality(cues, audio_path=None):
    """给每个能量窗口算一个「可信度」0~1，供 DP 加权。

    质量 = 0.5×长度分 + 0.5×安静分：
      · 长度分：≥400ms 满分，150ms 约 0.38
      · 安静分：窗口内最静帧比人声线再低 30dB 记满分（真静音）
    ★ 为什么不能只看长度：S03 实测 34.60-34.75s 只有 150ms，但它是**数字静音**
      （−90dB）；而 30.47-31.26s 有 790ms，最深也只到 −60dB 左右。
      只看长度会把前者误判成弱窗口、把后者高估。
    返回 [(起秒, 止秒, 质量)]。
    """
    import array
    import math

    if not audio_path or not os.path.isfile(audio_path):
        return []
    wins = pause_windows(audio_path)
    if not wins:
        return []
    try:
        p = subprocess.run(
            [FF, "-nostdin", "-v", "quiet", "-i", audio_path, "-ac", "1", "-ar", "8000",
             "-f", "s16le", "-"], capture_output=True)
        pcm = array.array("h")
        pcm.frombytes(p.stdout)
    except Exception:
        return [(a, b, 0.5) for a, b in wins]
    if not pcm:
        return [(a, b, 0.5) for a, b in wins]

    win = 80          # 10ms @8kHz
    n = len(pcm) // win

    def db_of(i):
        base = i * win
        s = 0.0
        for j in range(base, min(base + win, len(pcm))):
            v = pcm[j]
            s += v * v
        rms = math.sqrt(s / win) / 32768.0
        return 20 * math.log10(rms) if rms > 1e-10 else -90.0

    out = []
    for a, b in wins:
        len_score = min(1.0, (b - a) / 0.40)
        i0 = int(a * 100)
        i1 = max(int(b * 100), i0 + 1)
        deepest = min((db_of(i) for i in range(i0, min(i1, n))), default=-90.0)
        # 人声线约 p90−20 ≈ −41dB；再低 30dB（−71dB 以下）算真静音
        quiet_score = min(1.0, max(0.0, (-41.0 - deepest) / 30.0))
        out.append((a, b, round(0.5 * len_score + 0.5 * quiet_score, 3)))
    return out


# 统一容差：±6 帧 = 0.25s @24fps。
# ★ 为什么统一到 6 而不是 2：`cut_quality(tol=0.25)` 一直用的是 ±6 帧，
#   为了不悄悄改变既有判定口径，候选池也用 ±6。想收紧用 SEG_CUT_TOL 环境变量。
CUT_TOL_FRAMES = int(os.environ.get("SEG_CUT_TOL", "6"))


def pause_pool(cues, audio_path=None, use_energy=None, tol=None):
    """★ 切点候选池 = `SRT cue 间隙` ∪ `音频能量停顿`，统一返回**秒**区间列表。

    · `use_energy` 默认看环境变量 `SEG_ENERGY`（默认开）。
      设 `SEG_ENERGY=0` 可退回「只看 SRT」的旧行为，便于 A/B。
    · `tol` 是卡点容差（帧），默认 6。
    · 返回的窗口已按 tol 向外扩过，调用方直接 `a <= t <= b` 判断即可。

    ⚠️ SRT 窗口按 0.20s 阈值取（与旧 `cut_quality` 完全一致，不改既有语义）；
       能量窗口没有长度阈值过滤 —— 它已经在 `min_sec=0.15` 那层筛过了，
       再用 0.20 砍一遍等于把刚补回来的短换气又扔掉。
    """
    if tol is None:
        tol = CUT_TOL_FRAMES
    pad = tol / FPS

    wins = []
    for i in range(len(cues) - 1):
        g0, g1 = cues[i][1], cues[i + 1][0]
        if g1 - g0 >= 0.20:
            wins.append((g0 - pad, g1 + pad))

    if use_energy is None:
        use_energy = (os.environ.get("SEG_ENERGY") or "1").strip().lower() \
            not in ("0", "false", "off", "no")
    if use_energy:
        for a, b in pause_windows(audio_path):
            wins.append((a - pad, b + pad))

    return wins


def pause_pool_q(cues, audio_path=None, use_energy=None, tol=0):
    """同 `pause_pool`，但每个窗口带**质量权重** 0~1（供 DP 加权用）。

    SRT 间隙没有能量信息 → 给 1.0（语义切分，天然可信）。
    能量窗口用 `pause_quality()` 的分数：短的、不够静的会拿低分。
    ★ 意义：DP 有多个「都能落停顿」的方案时，优先挑**停顿更可靠**的那个，
      而不是单纯挑离均分最近的。S03 实测短窗口质量 0.69-0.70，
      长窗口 1.00 —— 加权后 DP 不会为了省几十帧去吃一个 150ms 的浅间隙。
    """
    if use_energy is None:
        use_energy = (os.environ.get("SEG_ENERGY") or "1").strip().lower() \
            not in ("0", "false", "off", "no")
    pad = tol / FPS
    out = []
    for i in range(len(cues) - 1):
        g0, g1 = cues[i][1], cues[i + 1][0]
        if g1 - g0 >= 0.20:
            out.append((g0 - pad, g1 + pad, 1.0))
    if use_energy:
        for a, b, q in pause_quality(cues, audio_path):
            out.append((a - pad, b + pad, q))
    return out


def plan_segments(total_frames, srt_cues, n_seg):
    """自动分段（DP）。切点优先落在句间停顿里 —— 宁可在停顿处切，也不要在半句上切。

    ★ 数学结构（推导错一次就必然撞 validate，别凭感觉写）
    ------------------------------------------------------------------
    段长 L_i = 5+17n，相邻重叠固定 OVERLAP（OVERLAP ≡ 5 mod 17，当前定板 5）。
    第 i 段起点 b_i 满足 b_{i+1} = b_i + L_i - OVERLAP，
    于是 b_{i+1} - b_i = L_i - OVERLAP ≡ 5 - 5 ≡ 0 (mod 17)。
    → **所有段起点必须是 17 的倍数**。
    → 相邻起点间距 d ∈ [17, MAXLEN-OVERLAP]：d=17 时 L=17+OVERLAP，
      d=MAXLEN-OVERLAP 时段长正好封顶。
      ⚠️ 上限必须跟着 MAXLEN 与 OVERLAP 一起走：硬编码 170 只在
      MAXLEN=192、OVERLAP=22 时成立。1.33MP（MAXLEN=141）时若仍允许 d=170，
      就会排出一段 192 帧超长段。
    → 最后一段 L = total - b_{n-1}；因为 total ≡ 5 (mod 17) 且 b ≡ 0，天然合法，
      只需卡 5 ≤ total-b ≤ MAXLEN。

    所以问题化为：在「17 的倍数」这个格点上放 n-1 个边界，间距 ≤ MAXLEN-OVERLAP，
    终点余量 ≤MAXLEN，代价 = 偏离均分位置的距离 − 落在停顿里的小奖励。用 DP 求最优。
    """
    maxd = MAXLEN - OVERLAP           # 相邻起点最大间距（决定段长上限）
    total = total_frames
    n = max(n_seg, min_segs(total))
    if n == 1:
        return [(0, total)]

    # 停顿时段（帧）：2026-10-27 起取 `SRT cue 间隙` ∪ `音频能量停顿`。
    # 旧写法只看 SRT 且门槛 0.20s，会把句中气口和短换气整片漏掉；
    # 实测 S03 并集后 6 段可行域从 11 种涨到 105 种（9.5 倍）。
    # ★ 带质量权重：优先挑「停顿更可靠」的落点，不是单纯离均分最近的。
    gaps_q = [(a * FPS, b * FPS, q)
              for a, b, q in pause_pool_q(srt_cues, AUDIO_SRC, tol=0)]

    def cost(i, pos):
        # 理想均分位置：等长时 b_i = i*(total-OVERLAP)/n
        ideal = (total - OVERLAP) * i / n
        c = abs(pos - ideal)
        # 落在停顿里给奖励，奖励随窗口质量缩放（0.69 的浅间隙只给 ~31 分）
        for a, b, q in gaps_q:
            if a - CUT_TOL_FRAMES <= pos <= b + CUT_TOL_FRAMES:
                c -= 45 * q
                break
        return c

    lo_last = total - MAXLEN          # 最后一个边界不能早于这里，否则末段超长
    cand = list(range(STEP, total - 4, STEP))

    INF = float("inf")
    layers = []                       # layers[i-1] = 第 i 个边界的 {位置: (累计代价, 前驱)}
    prev = {0: (0.0, None)}
    for i in range(1, n):
        cur = {}
        for pos in cand:
            if i == n - 1 and pos < lo_last:
                continue
            best, arg = INF, None
            for pv, (cst, _) in prev.items():
                d = pos - pv
                if d < STEP or d > maxd:
                    continue
                v = cst + cost(i, pos)
                if v < best:
                    best, arg = v, pv
            if arg is not None:
                cur[pos] = (best, arg)
        if not cur:
            raise RuntimeError(f"分段无解：total={total} n={n}（间距约束过紧）")
        prev = cur
        layers.append(cur)

    # 收尾：末段长度必须合法
    best, last = INF, None
    for pos, (cst, _) in prev.items():
        L = total - pos
        if L < 5 or L > MAXLEN:
            continue
        if cst < best:
            best, last = cst, pos
    if last is None:
        raise RuntimeError(f"末段无合法长度：total={total} n={n}")

    bounds = [last]
    for i in range(n - 1, 1, -1):
        bounds.append(layers[i - 1][bounds[-1]][1])
    bounds.reverse()                  # n-1 个边界，升序

    starts = [0] + bounds
    segs = []
    for i, s in enumerate(starts):
        L = (starts[i + 1] - s + OVERLAP) if i + 1 < len(starts) else (total - s)
        segs.append((s, s + L))
    return segs


def validate(segs):
    prev_end = 0
    tot = 0
    for i, (s, e) in enumerate(segs):
        L = e - s
        assert legal_len(L), f"段{i+1} 长 {L} 非法（须 5+17n 且 ≤{MAXLEN}）"
        if i:
            ov = prev_end - s
            assert ov % 17 == 5, f"段{i+1} 重叠 {ov} 非法（须 ≡5 mod 17）"
            assert ov < min(L, segs[i - 1][1] - segs[i - 1][0]), "重叠不小于段长"
            tot += L - ov
        else:
            tot += L
        prev_end = e
    return tot


def cut_quality(segs, cues, tol=0.25, audio=None, energy=True):
    """接缝质量自检：每个切点是否落在句间停顿里。

    ★ 为什么必须查（2026-10-27 加）
    重叠帧从 22 改到 5 之后，`maxd = MAXLEN-OVERLAP` 变宽了，但 DP 的均分目标
    `ideal = (total-OVERLAP)*i/n` 也跟着变 → 段数不变时切点会整体平移。
    S03 实测：OVERLAP=5 且 TARGET_SEGS=5（默认）→ 5 段，
    帧 187（7.79s）**切在半句上**，偏离最近停顿 528ms。
    这类问题 DRY 只打印分段表是看不出来的（表里数字全都"合法"），
    必须拿停顿窗口逐点比对，否则要等片出来听感发虚才发现。

    ★ 2026-10-27 起改用 `pause_pool()`：**SRT ∪ 能量**，不再只看 SRT。
    实测 S03 已交付的 5 个切点里，748 号距最近 SRT 窗口差 3.0 帧（严格 ±2 判失败），
    但能量窗口 30.47-31.26s（帧 731-750）确实含住它、余量 2.2 帧。
    **是 SRT 窗口画窄了，不是切点选错了** —— 只看 SRT 会把这种好切点误判成失败。

    ⚠️ `energy=False` 时只看 SRT 窗口 —— 这是**旧口径**，留作对照：
    能量法对 SRT 时间戳错误是鲁棒的（被拒版 19 条 cue 整体漂移，
    能量法照样找到同一批真实停顿），所以「切点全落停顿」**不再能证明 SRT 没出错**。
    想看字幕本身准不准，必须另外跑 `04_验收工具/srt_sync_audit.py`。

    返回 (落在停顿数, 接缝总数, 半句切点列表[(帧, 秒)])
    """
    if len(segs) < 2:
        return 0, 0, []
    if audio is None:
        audio = AUDIO_SRC
    pool = pause_pool(cues, audio, tol=round(tol * FPS), use_energy=energy)
    cuts = [s for s, _ in segs][1:]
    hit, bad = 0, []
    for c in cuts:
        t = c / FPS
        if any(a <= t <= b for a, b in pool):
            hit += 1
        else:
            bad.append((c, t))
    return hit, len(cuts), bad


def print_cut_quality(segs, cues):
    hit, n, bad = cut_quality(segs, cues)
    if not n:
        return True
    # ★ 双口径对照：能量法会掩盖 SRT 时间戳错误，所以必须同时报「只看 SRT」的结果。
    #   被拒版实测：19 条 cue 整体漂移，能量法照样找到同一批真实停顿，
    #   于是并集口径报 5/5 —— 但字幕本身是错的。单看并集就看不出问题。
    hit_s, _n2, bad_s = cut_quality(segs, cues, energy=False)
    mark = ""
    if hit_s < hit:
        mark = f"  ← 只看 SRT 是 {hit_s}/{n}，差的 {hit - hit_s} 个靠能量窗救回"
    print(f"   切点落停顿 {hit}/{n}（SRT∪能量）｜只看 SRT {hit_s}/{n}{mark}")
    if hit_s < hit:
        for c, t in bad_s:
            if c not in [x for x, _ in bad]:
                print(f"      帧{c}({t:.2f}s) SRT 里没有间隙，但音频能量有真停顿")
    # ★★ 这条警告必须常驻 —— 实测过一个反直觉的坑：
    #   被拒版（19 条 cue，时间戳整体漂移 ≈ 错 0.9s）反而报「只看 SRT 5/5」，
    #   而干净的定版报 4/5。原因：坏 SRT 把句子切得更碎、间隙更多，
    #   于是任何切点都更容易「撞上」某个间隙。**指标好 ≠ SRT 对。**
    print("      ⚠ 切点质量不能用来验收 SRT 本身（坏 SRT 反而更容易蒙对）。"
          "字幕准不准只认 srt_sync_audit.py")
    if bad:
        print(f"   ⚠ 切点落停顿 {hit}/{n}  半句上：" +
              " ".join(f"帧{c}({t:.2f}s)" for c, t in bad))
        # ★ 直接给出「可用的停顿窗口」，比让人自己翻 SRT 快得多。
        #   手动定长时这就是调时长的依据：把这段的长度改到让切点落进下面任一个窗口。
        #   2026-10-27 起窗口取自 `pause_pool()`（SRT ∪ 能量），与判定口径一致。
        gaps = [(a + CUT_TOL_FRAMES / FPS, b - CUT_TOL_FRAMES / FPS)
                for a, b in pause_pool(cues, AUDIO_SRC)]
        gaps = [(a, b) for a, b in gaps if b >= a]
        k = len(segs)
        for idx, (c, t) in enumerate(bad, start=1):
            # 这个切点是第 idx 个接缝 → 它是前 idx 段长度的累加结果。
            # ★ 关键：切点只能落在**整 17 的倍数**上，因为
            #     c_i = ΣL_j − i·OVERLAP，而 L_j≡5、OVERLAP≡5 → c_i ≡ 17Σn ≡ 0 (mod 17)。
            #   所以「挪到 6.56s（−30 帧）」这种建议根本落不下去（−30 不是 17 的倍数，
            #   段长改 30 帧会被 snap 成 34 或 17）。必须只推荐 17 的倍数。
            cands = []
            for ga, gb in gaps:
                # gaps 里是**秒**，切点是**帧**，先统一到帧
                fa = int(round(ga * FPS))
                fb = int(round(gb * FPS))
                # 每个停顿窗口只取**该窗口内最靠近 c 的格点**（各一个），
                # 否则会把很久以前的格点也收进来，给出「接缝2 挪到 15.58s」
                # 这种跨 13 格、已经完全不是这个接缝的建议。
                in_win = [x for x in range(max(0, fa - 6), fb + 7) if x % 17 == 0]
                if in_win:
                    cands.append(min(in_win, key=lambda x: abs(x - c)))
            cands = sorted(set(cands), key=lambda x: abs(x - c))
            usable = []
            for x in cands:
                if x == c:
                    continue
                # 本段长度 = x − 上一段起点；若 idx==1 则是 x 本身
                prev_s = segs[idx - 1][0]
                newL = x - prev_s
                if not (5 <= newL <= MAXLEN):
                    continue
                # 下一段会跟着平移：它的起点变成 x − OVERLAP，长度不变，故恒安全。
                # 但若这是最后一个接缝，末段长度 = total − (x − OVERLAP)，要检查。
                if idx == k - 1:
                    lastL = segs[-1][1] - (x - OVERLAP)
                    if not (5 <= lastL <= MAXLEN):
                        continue
                # 一次最多挪 3 格，超过就不算「微调」而是重新规划了
                dev = x - c
                if abs(dev) > 3 * 17:
                    continue
                usable.append((abs(dev), x, dev, newL))
            usable.sort()
            # 同一个目标帧可能被相邻两个窗口各算一次 → 按 x 去重
            seen, tips = set(), []
            for _ad, x, dev, newL in usable:
                if x in seen:
                    continue
                seen.add(x)
                tips.append(f"{x/FPS:.2f}s（{dev//17:+d} 格 → 本段长 {newL} 帧）")
                if len(tips) == 2:
                    break
            if tips:
                print(f"      接缝{idx} 帧{c} 可改到：" + "；".join(tips))
            else:
                # 说明为什么改不动：通常是「上段已经顶到 MAXLEN」
                prevL = segs[idx - 1][1] - segs[idx - 1][0]
                why = (f"上段已长 {prevL} 帧（MAXLEN={MAXLEN}）"
                       if prevL >= MAXLEN else f"±3 格内无格点落在停顿")
                print(f"      接缝{idx} 帧{c} 改不动：{why} → 这一段只能这样，"
                      f"或换段数/换 SEG_DURS")
        print(f"      → 换段数最省事：TARGET_SEGS=+1/-1；也可 SEG_DURS 直接给各段秒数")
        return False
    return True


def parse_seg_override(spec, total):
    """把 `SEG_OVERRIDE` 字符串解析成段表，并做全套硬约束校验。

    ★ 为什么需要它（2026-10-27）
    自动分段的核心是「等长均分 + 切点吸向停顿」。但业务上经常**不需要均分**：
    某段想多说一句就拉长、某段只是过渡就压短，或者就是想让某个切点必须落在
    某个字之后。等长均分在这种需求下无解 —— 它会为了「长度接近」牺牲切点位置。

    语法（两段式，`起点-终点`）：
        SEG_OVERRIDE="0-192,187-379,374-549,544-906"
    也接受只写起点、由脚本顺推终点（更省事，不容易写错）：
        SEG_OVERRIDE="0,187,374,544"     # 终点 = 下一段起点 + OVERLAP，末段到 total
    或用环境变量 `SEG_OVERLAP` 临时改重叠（逐段不等长的场合常用）。

    无论哪种写法，都会强制校验采样器的真实约束（`minimax_h3_finite_segments.py:458`）：
      · 首段必须从 0 开始
      · 每段 `L ≡ 5 (mod 17)`，且 `5 ≤ L ≤ 3592`
      · 相邻段 `重叠 ≡ 5 (mod 17)`，且 `0 < 重叠 < min(前段长, 本段长)`
      · 每段必须严格向前推进（`start(i+1) > start(i)` 且 `end(i+1) > end(i)`）
      · 末段终点必须等于 total
    """
    parts = [x.strip() for x in spec.split(",") if x.strip()]
    if not parts:
        raise ValueError("SEG_OVERRIDE 为空")

    raw = []
    for p in parts:
        if "-" in p:
            a, b = p.split("-", 1)
            raw.append([int(a), int(b)])
        else:
            raw.append([int(p), None])

    # 只给了起点 → 顺推终点
    for i, item in enumerate(raw):
        if item[1] is None:
            if i + 1 < len(raw):
                item[1] = raw[i + 1][0] + OVERLAP
            else:
                item[1] = total
    segs = [(a, b) for a, b in raw]

    # ---- 全套校验（任何一条不满足，采样器都会在跑了几分钟之后才报错）
    errs = []
    if segs[0][0] != 0:
        errs.append(f"首段必须从 0 开始，实际 {segs[0][0]}")
    for i, (s, e) in enumerate(segs):
        L = e - s
        # ⚠️ 上限用 MAXLEN（随分辨率收紧的**本机显存安全线**），不是采样器的 3592。
        #   3592 是采样器允许的理论上限，1.0MP 下超过 192 帧在本机必 OOM。
        if L < 5 or (L - 5) % 17 or L > MAXLEN:
            near = 5 + 17 * round((L - 5) / 17)
            near = min(max(near, 5), MAXLEN)
            errs.append(f"段{i+1} 长 {L} 非法（须 5+17n 且 ≤MAXLEN={MAXLEN}，"
                        f"最近的合法值是 {near}）")
        if i:
            ps, pe = segs[i - 1]
            ov = pe - s
            if s <= ps:
                errs.append(f"段{i+1} 起点 {s} 未推进（≤上段起点 {ps}）")
            if e <= pe:
                errs.append(f"段{i+1} 终点 {e} 未推进（≤上段终点 {pe}）")
            if ov <= 0:
                errs.append(f"段{i+1} 重叠 {ov} 非法（须 >0）")
            elif ov % 17 != 5:
                errs.append(f"段{i+1} 重叠 {ov} 非法（须 ≡5 mod 17，"
                            f"可选 {ov - (ov % 17 - 5) if ov % 17 >= 5 else ov - ov % 17 - 12}）")
            elif ov >= min(L, pe - ps):
                errs.append(f"段{i+1} 重叠 {ov} ≥ 较短段长")
    if segs[-1][1] != total:
        errs.append(f"末段终点 {segs[-1][1]} ≠ total {total}（差 {total - segs[-1][1]} 帧）")
    if errs:
        raise ValueError("SEG_OVERRIDE 校验失败：\n  - " + "\n  - ".join(errs))
    return segs


def parse_seg_grid(spec, total):
    """★ 按「17 格数」直接算切点 —— 一步到位，不需要试。

    ★ 为什么这是最正确的写法（2026-10-27 用户指出）
    用户原话：「每一段分配时长是在乘以 17 的基础上，按照规则来的，
             我们可以算出一个切点的时间，并且刚好在乘以 17 的卡点上吗。」

    **可以，而且是恒等式，不是巧合。** 推导：
        段长  L_i = 5 + 17·n_i          （n_i 就是「格数」）
        切点  c_i = Σ_{j≤i} L_j − i·重叠
                  = Σ(5+17n_j) − 5i     （重叠取 5）
                  = 5i + 17Σn_j − 5i
                  = 17 · Σn_j           ← 5i 与重叠的 5i 恰好抵消！
    ⇒ **切点 = 17 × 前 i 段格数之和**。恒在 17 卡点上，且不需要任何搜索。

    这也解释了为什么 `SEG_DURS` 需要「吸附 + 成对交换」那套麻烦逻辑：
    它从「秒数」出发，要先折算成格数、再凑总长，还得回头救切点。
    而从**格数**出发，一切天然成立：
        c_i = 17 × cumsum(n)[i]
        末段格数 n_k = (total − 5(k−1) − 17Σn) / 17  … 由总长反推，必然整除
      因为 total ≡ 5 (mod 17)、每个 L≡5 ⇒ 余量天然落在 5+17n 上。

    语法（逗号分隔每段格数，最后一格可写 `?` 让脚本按总长补齐）：
        SEG_GRID="9,10,7,7,11,4"      # 六段，格数；和 = 48
        SEG_GRID="9,10,7,7,11,?"      # 末段自动补齐
    约束：每段格数 n ≥ 1（L=5+17n ≥ 22），且 L ≤ MAXLEN ⇒ n ≤ (MAXLEN−5)//17。

    校验（会逐条报错，不会静默）：
        · 格数必须是正整数
        · 总量必须恰好等于 total（或末段写 ? 自动补）
        · 每段 L 必须 ≤ MAXLEN
    """
    parts = [x.strip() for x in spec.split(",") if x.strip()]
    if not parts:
        raise ValueError("SEG_GRID 为空")
    NMAX = (MAXLEN - 5) // 17

    # 末段写 ? → 按总长反推
    tail_q = parts[-1] == "?"
    nums = parts[:-1] if tail_q else parts
    try:
        n = [int(x) for x in nums]
    except ValueError:
        raise ValueError(f"SEG_GRID 只能写整数格数，实际 {spec}")
    if any(x < 1 for x in n):
        raise ValueError(f"SEG_GRID 每段格数须 ≥1（段长 5+17n），实际 {n}")

    k = len(parts)
    if tail_q:
        # total = ΣL − 重叠(k−1) = 5k + 17Σn − 5(k−1) = 17Σn + 5
        used = sum(n)
        need = (total - 5) // 17
        if (total - 5) % 17:
            raise ValueError(f"净长 {total} 不是 5+17n 形式，SEG_GRID 无法精确整除")
        last = need - used
        if last < 1:
            raise ValueError(
                f"SEG_GRID 前 {k-1} 段已用 {used} 格，超过总格数 {need}"
                f"（净长 {total} 帧）；末段至少要 1 格")
        n.append(last)
    else:
        if len(n) != k:
            raise ValueError(f"SEG_GRID 段数不一致（{len(parts)} 段但解析到 {len(n)} 个）")
        if sum(n) * 17 + 5 != total:
            raise ValueError(
                f"SEG_GRID 格数之和 = {sum(n)}，对应净长 {sum(n)*17+5} 帧，"
                f"但本片需要 {total} 帧（差 {sum(n)*17+5-total:+d} 帧）。\n"
                f"  算式：净长 = 17×Σn + 5。要么改格数，要么末段写 ? 让脚本补齐。")

    for i, x in enumerate(n):
        if x > NMAX:
            raise ValueError(
                f"段{i+1} 格数 {x} 超过上限 {NMAX}（段长 {5+17*x} > MAXLEN={MAXLEN}）")

    # ★ 一步算出切点：c_i = 17 × 前 i 段格数之和
    segs, acc = [], 0
    prev_c = 0
    for i, x in enumerate(n):
        acc += x
        c = 17 * acc                    # 切点恒在 17 卡点上（恒等式）
        end = total if i == k - 1 else c + OVERLAP
        segs.append((prev_c, end))
        prev_c = c

    # 恒等式自检：任何一段都必须满足 L≡5(mod17)，否则说明推导被改坏了
    for i, (s, e) in enumerate(segs):
        if (e - s - 5) % 17:
            raise ValueError(
                f"内部错误：段{i+1} 长 {e-s} 不满足 5+17n —— 切点公式失效，请检查 OVERLAP")
    return segs, n


def snap_seg_override(durations_sec, total, cues=None):
    """按「每段目标**秒数**」自动排出一张合法的分段表（不做均分，只做吸附）。

    ★ 为什么有这个东西（2026-10-27）
    手写 `SEG_OVERRIDE` 要同时满足五条硬约束，人手算几乎一定会差几帧
    （实测连续写错 3 次）。所以给你两条路：
      · `SEG_OVERRIDE`       —— 精确控制，写错就报错，适合已经算好的表
      · `SEG_DURS`           —— 只给每段想要的**秒数**，脚本自己吸附
    后者才是「按业务需要控制每段时长」的常用写法：
        SEG_DURS="8,7,6,6,6,5"        # 六段，每段想这么长

    ★ 吸附时会**优先挑能落进句间停顿的合法长度**。
    为什么必须这样：手动定长之后，切点位置完全由「你给的秒数」决定，
    不再有均分逻辑帮你避让 → 实测 `8,7,6,6,6,5` 直接排出 0/5 全切半句。
    在不改变段数的前提下，段长仍有多个合法值（5+17n），
    挑一个让终点落进停顿的，既不违背你的时长意图，又能救回切点。
    数组越长（段越多），能腾挪的合法长度组合越多，越容易全对上。

    返回值或抛 ValueError。
    """
    if not durations_sec:
        raise ValueError("SEG_DURS 为空")

    gaps = []
    if cues:
        # 2026-10-27 起与 cut_quality 同源：SRT 间隙 ∪ 能量停顿
        gaps = pause_pool(cues, AUDIO_SRC, tol=0)

    def in_gap(frame, tol=0.25):
        t = frame / FPS
        return any(a - tol <= t <= b + tol for a, b in gaps)

    # 逐段定长。末段也按目标时长定，**不让它吃余量** ——
    # 否则末段常常长到超过 MAXLEN（实测 8,7,6,6,6,5 末段 226 帧 > 192）。
    # 余量/欠量由最后统一校正：把差额摊到各段的合法长度上。
    def seg_L(s, want):
        """给起点 s、目标长度 want，返回吸附后的合法 L（优先落停顿）。"""
        base = snap(want, lo=5, hi=MAXLEN)
        cands = []
        for k in (-2, -1, 0, 1, 2):
            L = base + 17 * k
            if 5 <= L <= MAXLEN:
                cands.append(L)
        if not cands:
            cands = [base]
        # 落在停顿里的优先；其次靠近目标
        cands.sort(key=lambda L: (0 if in_gap(s + L) else 1, abs(L - want)))
        return cands[0]

    # 用「17 帧为单位的额外量」精确凑到 total。
    # 为什么不能用浮点秒去微调：段长只能落在 5+17n 格点上，
    # 浮点加 0.1s 之类会被 snap 掉，差额永远补不上（实测 5 段永远差 14 帧）。
    # 所以直接按整数格点分配：net = total ⇔ 17*Σn_i + 5*k − OVERLAP*(k−1) = total。
    k = len(durations_sec)
    n_list = []
    for i, d in enumerate(durations_sec):
        L0 = snap(int(round(d * FPS)), lo=5, hi=MAXLEN)
        n_list.append((L0 - 5) // 17)

    # ★ 你说要的总长和片长差很多时必须明说，否则脚本会「自动压缩/拉伸」
    #   而输出里只看得到「实际 8.00s（-1.00s）」这种逐段小字，容易误以为没事。
    #   实测 SEG_DURS="9,9,9,9,9"（要 45s）在 37.75s 的片上被压成 5 段 8s。
    #   ⚠️ 必须用**原始诉求**（d*FPS）比对，不能用 n_list —— 后者已被 MAXLEN 封顶，
    #      45s 的诉求在 n_list 里只剩 34 帧差，看不出问题有多严重。
    _raw_net = sum(int(round(d * FPS)) for d in durations_sec) - OVERLAP * (k - 1)
    if abs(_raw_net - total) > 17 * k:
        _d = _raw_net - total
        print(f"   ⚠ 你要的总长 {_raw_net} 帧（{_raw_net/FPS:.2f}s）与片长 "
              f"{total} 帧（{total/FPS:.2f}s）相差 {_d:+d} 帧（{_d/FPS:+.2f}s），"
              f"脚本会把差额摊到各段上 —— 逐段实际时长见下面的段表。")

    # 先按目标算一次净长，再逐个格点补差
    def net_of(nl):
        return sum(5 + 17 * n for n in nl) - OVERLAP * (k - 1)

    target_net = total
    guard = 0
    while net_of(n_list) != target_net and guard < 5000:
        guard += 1
        cur = net_of(n_list)
        # 差得太远就大跨步，接近了再一格一格补（避免来回抖）
        need = target_net - cur
        if need % 17 != 0:
            # total 与 OVERLAP 决定的可达集：净长 ≡ 5 − OVERLAP(k−1) (mod 17)，
            # 与 total 天然同余（total 也是 5+17m）。若真不齐，交给下面报错。
            break
        step = need // 17
        idx_order = list(range(k - 1, -1, -1)) if step > 0 else list(range(k))
        applied = False
        for idx in idx_order:
            if step == 0:
                break
            d_n = 1 if step > 0 else -1
            new_n = n_list[idx] + d_n
            if 0 <= new_n <= (MAXLEN - 5) // 17:
                # 一次别挪太多，逐格检查停顿优先
                n_list[idx] = new_n
                step -= d_n
                applied = True
        if not applied:
            break

    # 按最终格点生成段表。
    # ⚠️ 这里**必须**直接用 5+17n 的精确值，不能再调 seg_L() 去「优化」——
    #    上面刚用整数格点把总和凑得刚好，任何一次重新挑选都会破坏总和
    #    （实测：重新挑完末段永远变成 226 帧 > 192）。
    #    停顿偏好改为在下面用「保总和的成对交换」来处理。
    def build(nl):
        st = [0]
        out = []
        for i in range(k):
            s = st[-1]
            e = s + 5 + 17 * nl[i]
            out.append((s, e))
            st.append(e - OVERLAP)
        out[-1] = (out[-1][0], total)      # 末段收口，消除累计误差
        return out

    segs = build(n_list)

    # 停顿偏好：成对交换（一段 +17、另一段 −17）不改变总和，只改变切点位置。
    # 贪心做几轮，能多落一个停顿就换。
    def gap_hits(nl):
        sg = build(nl)
        h, _n, _b = cut_quality(sg, cues or [])
        return h

    if cues:
        # 偏离上限：成对交换虽不改总和，但会把某段推得离你的意图很远
        # （实测「第5段 6s」被推到 4.46s，用户意图就丢了）。
        # ★ 两种模式，用 SEG_PRIOR 选（2026-10-27 加）：
        #   SEG_PRIOR=cut（默认）→ 切点优先：只受 MAXDEV 限制，尽量让切点全落停顿
        #   SEG_PRIOR=dur          → 时长优先：**完全不做交换**，严格按你给的秒数排
        #     适合「这段必须说到某句为止」的业务需求，代价是切点可能落在半句上。
        PRIOR = (os.environ.get("SEG_PRIOR") or "cut").strip().lower()
        MAXDEV = int(os.environ.get("SEG_MAXDEV", str(3 * 17)))   # 默认 51 帧 ≈ 2.1s
        if PRIOR == "dur":
            MAXDEV = 0        # dev_of 恒 ≤0 才允许，等于禁止任何交换
        want_L = [5 + 17 * ((snap(int(round(d * FPS)), lo=5, hi=MAXLEN) - 5) // 17)
                  for d in durations_sec]

        def dev_of(nl):
            return max(abs((5 + 17 * nl[i]) - want_L[i]) for i in range(k))

        base_hits = gap_hits(n_list)
        base_dev = dev_of(n_list)
        NMAX = (MAXLEN - 5) // 17
        # ★ 允许一次挪 m 格（原来只挪 1 格）。原因：切点只能落在整 17 的倍数上，
        #   而「让某个接缝跳进下一个停顿窗口」往往需要一次跨 2~4 格。
        #   单步贪心在中间态不改善时会立刻停住 —— SEG_DURS="8,6,6,6,6,6" 卡在 3/5
        #   就是这么来的（首段顶到 192 后，接缝1 被钉在 187，要跳到 204 需 +1 格
        #   但 204 > MAXLEN，可用的是 221(13格) 或回退 153，单步都够不到）。
        for _round in range(3):
            improved = False
            for i in range(k):
                for j in range(k):
                    if i == j:
                        continue
                    for m in (1, 2, 3, 4):
                        cand = list(n_list)
                        if cand[i] + m > NMAX or cand[j] - m < 0:
                            continue
                        cand[i] += m
                        cand[j] -= m
                        if dev_of(cand) > MAXDEV:
                            continue
                        h = gap_hits(cand)
                        # 判据：停顿数更多，或停顿数持平但偏离更小
                        if h > base_hits or (h == base_hits and dev_of(cand) < base_dev):
                            n_list, base_hits, base_dev = cand, h, dev_of(cand)
                            improved = True
                            break
            if not improved:
                break
        segs = build(n_list)
        if PRIOR != "dur" and base_dev > MAXDEV:
            print(f"   ⚠ 部分段偏离目标 {base_dev/FPS:.2f}s（超过 {MAXDEV/FPS:.1f}s 上限），"
                  f"是为了让切点落在停顿上；可用 SEG_MAXDEV=<帧> 放宽")
    # 末段长度若超 MAXLEN，靠前面几段压不掉就是段数不够，明确报出来
    L_last = segs[-1][1] - segs[-1][0]
    if L_last > MAXLEN or any((e - s) > MAXLEN for s, e in segs):
        worst = max(segs, key=lambda se: se[1] - se[0])
        # 理论上最少需要几段：n*MAXLEN - (n-1)*OVERLAP >= total
        nmin = 1
        while nmin * MAXLEN - (nmin - 1) * OVERLAP < total:
            nmin += 1
        raise ValueError(
            f"SEG_DURS 装不下：某段长到 {worst[1]-worst[0]} 帧，超过 MAXLEN={MAXLEN}。\n"
            f"  {len(durations_sec)} 段最多只能装 "
            f"{len(durations_sec) * MAXLEN - (len(durations_sec)-1) * OVERLAP} 帧，"
            f"而本片需要 {total} 帧。\n"
            f"  → 至少给 {nmin} 段，或多给几段再试。")
    return segs


# ---------------------------------------------------------------- 提示词

GESTURES = [
    "a slow blink lands on the last word and his gaze stays level on the lens",
    "his head tilts a fraction down and back up, one small furrow of thought",
    "one small open-palm turn near his lap, low in frame, the wrist loose",
    "his eyebrows lift a little and the corner of his mouth curves",
    "one slow clear nod, his shoulders settling",
    "his head tips slightly to one side, one almost imperceptible shrug",
    "his open palm presses very gently toward the lens once, then returns to rest",
    "his chin lifts slightly and his eyes hold the lens a beat longer",
    "his smile widens into a warm genuine grin that stays to the final frame",
]

# ★ 2026-10-28 用户要求：除第 1–3 段外，其余各段表情「收敛」一些。
#   根因：GESTURES 里 index 3（挑眉 + 嘴角上扬）和 index 8（开朗大笑）会驱动模型
#   在那些段落把抬头纹 / 法令纹渲得偏深。这里给后段一套「平静版」手势，只换掉
#   这两个表情驱动项（其余保持原样），并配合 build_prompt 里的 calm_expr 从句。
#   段号映射：第 1/2/3 张 = i=0/1/2（保持原 GESTURES）；第 4 张起 = i>=3（用本表）。
CALM_GESTURES = [
    "a slow blink lands on the last word and his gaze stays level on the lens",
    "his head tilts a fraction down and back up, one small furrow of thought",
    "one small open-palm turn near his lap, low in frame, the wrist loose",
    "his gaze stays level on the lens and his mouth moves only a little as he speaks",
    "one slow clear nod, his shoulders settling",
    "his head tips slightly to one side, one almost imperceptible shrug",
    "his open palm presses very gently toward the lens once, then returns to rest",
    "his chin lifts slightly and his eyes hold the lens a beat longer",
    "his mouth rests in a soft even line and his expression stays composed to the final frame",
]


def ts(sec):
    return f"{int(sec // 60):02d}:{sec - int(sec // 60) * 60:06.3f}"


# ---------------------------------------------------------------------------
# ★★ 2026-10-29：把「谁的脸、什么屋子、什么配乐」从代码里抽到 ip.json
#
# 为什么必须抽：build_prompt 里原本写死了这个 IP 的黑发/无框眼镜/盘扣衬衫/领夹麦、
# 米色墙/右边绿植/左边落地灯、还有 88BPM 吉他配乐。这些**全是 IP 参数**，
# 却住在引擎代码里 → 换一个客户就得复制一份脚本再手改 → 铁律 3「每个 IP 一份脚本副本」，
# 到 50 个客户就是 50 份分叉，改一个 bug 要改 50 处。
#
# 现在结构是这样：
#   代码 = 算法 + 提示词**骨架**（ talking-head explainer 的分节结构不能动，
#           summary / clean_plate / retention_analysis 这些节是 H3 的受理格式）
#   ip.json 的 video.prompt_vars = 这个 IP 的**名词**（长什么样、屋里有什么、底下什么音）
#
# ⚠ 默认值必须保持**中性**（不写具体的人/衣/物，一律把描述交回 <Picture 1>）：
#   新 IP 若还没配 prompt_vars，这些词就会**顶替静帧成为画面的描述来源**，
#   等于把上一个客户的外貌和陈设灌给下一个客户——这是身份污染，比画质坏更严重。
#   IP 自己的值写在各自 ip.json 的 video.prompt_vars 里（同名覆盖）。
from string import Template

DEFAULT_PROMPT_VARS = {
    "who_lc": 'the presenter',
    "who_cap": 'The presenter',
    "lang": 'Mandarin',
    "appearance": (
        'his face and bone structure, his hairstyle, his outfit and every accessory '
        'exactly as they appear in <Picture 1>'
    ),
    "skin": (
        'His skin is lit by one very large diffused source close to him, as though '
        'through a broad white scrim, so the light wraps around his face and the '
        'transition from lit skin to shadow is gradual and broad. His skin reads as '
        'clean natural skin, matte and dry to the touch with visible pores and a '
        'faint natural texture; the brightest area on his forehead, nose bridge and '
        'cheeks reaches a clean bright value while the sides of his face and the '
        'area under his jaw fall into gentle shadow, so the light still reads as '
        'light and the face keeps a full rounded form rather than a flat even wash. '
        'Each lit area is filled with an even smooth sheen of the same value rather '
        'than a bright reflective patch, and his forehead matches the tone of his '
        'cheeks.'
    ),
    "brows": (
        'His brows sit naturally relaxed and smooth, the skin between them smooth '
        'and open with a soft flat brow line, the inner ends level with each other, '
        'and the whole brow arc resting in its natural position with the forehead '
        'above it smooth and uncreased.'
    ),
    "expression": (
        'His eyes stay clear and engaged and his expression stays warm, open and '
        'confident.'
    ),
    "scene_role": 'the room behind him in <Picture 1>',
    "scene_noun": 'room',
    "scene_objects": (
        'everything behind him exactly as it is in <Picture 1>, keeping the same '
        'objects in the same places and the same soft focus'
    ),
    "ret_objects": (
        'his identity, his face, his outfit and every accessory, the background '
        'behind him, the lighting and the colour tone'
    ),
    # ★★ 2026-10-31：把这四个「姿态」从骨架里拆出来（第 4 处同类护栏）。
    #   以前骨架写死 "his seated posture" / "in his chair, near his lap" / "sits"，
    #   等于**默认所有人都是坐着对着沙发讲话** —— 主号是坐姿，所以一直没暴露；
    #   珠珠（客户卡）是站在展厅里讲的，DRY 一跑就自相矛盾：
    #   subject_definitions 说 "her seated posture"，detailed 说 "standing"，
    #   模型被两句话拉扯，姿势会在段与段之间漂。
    #   默认值必须中性（画面交回 <Picture 1>），要用具体姿态的 IP 在 ip.json 里显式钉死。
    #   ⚠️ 主号 ip.json 已把前三个显式写成旧值，DRY 逐字节不变（有 golden 可 diff）。
    "posture": 'his posture',
    "rest_pose": 'settled and relaxed, hands resting low and still',
    # ★ 默认必须是「不表态」：坐着写 sits、站着写 stands，引擎不替客户选姿势。
    "pose_verb": 'is',
    "lap_ref": 'his lap',
    # ★ 原来默认 'seated in the room of the reference still' —— 等于「所有人都是坐着讲」。
    #   坐着讲的 IP 自己写 seated，站着讲的写 standing，别让引擎替客户决定姿势。
    "seated_where": 'in the room of the reference still',
    "where_of_still": 'in the room shown',
    # ★ <d> 标记里的语种标签。主号/珠珠是中文口播，但引擎不该把 [Chinese] 焊死 ——
    #   给英文或粤语 IP 出片时，这一处会让口型对不上（标记语言与实际发音不一致）。
    "lang_tag": 'Chinese',
    "above_head": (
        'the background above him continuing exactly as it does in <Picture 1>'
    ),
    "room_right": (
        'whatever sits on the right of frame in <Picture 1> is kept there'
    ),
    "room_left": 'whatever sits on the left edge in <Picture 1> is kept there',
    "wall_prop": 'The background',
    "edge_right": (
        'the right edge keeps whatever stands there in the reference still'
    ),
    "edge_left": (
        'the left edge keeps whatever stands there in the reference still'
    ),
    "torso": (
        'his clothing exactly as in <Picture 1>, keeping the same folds of fabric'
    ),
    "held_items": (
        'everything he wears and carries stays exactly as it is in <Picture 1>'
    ),
    "bg_kept": (
        'the same objects in the same places, with the same shapes and tones'
    ),
    "ambience": 'The quiet room tone of the room in the reference still',
    "music": (
        'A calm restrained instrumental bed with no vocals and no lyrics, sitting '
        'low under his voice with one steady level'
    ),
}

# ip.json 里写了的覆盖默认值；写了空字符串的按「没写」处理，避免意外清空成空。
PV = dict(DEFAULT_PROMPT_VARS)
_custom = IP.get("video.prompt_vars") or {}
PV.update({k: v for k, v in _custom.items() if isinstance(v, str) and v.strip()})
# 本 IP 的性别（male / female）。只影响骨架里 he/his 的归一，见 _gender_norm。
GENDER = str(IP.get("video.gender") or "male").strip()
if _custom:
    # ★ 下划线开头的是**元字段**（`_note` / `_gender_note` 这类给人看的说明），
    #   不是提示词变量，别混进"拼错的变量名"告警里 —— 否则每跑一次都刷一条假警告，
    #   真拼错时反而没人看（狼来了）。
    unknown = sorted(k for k in set(_custom) - set(DEFAULT_PROMPT_VARS)
                     if not k.startswith("_"))
    if unknown:
        # 拼错的变量名 Template 会原样留着不报错，等于改了个寂寞还查不出来
        print(f"  ⚠ ip.json 的 video.prompt_vars 里有不认识的名字：{unknown}（已忽略）")


# ★★ 2026-10-29：提示词闭环 —— 让工作台里改的稿子真的能出到片子上
#
# 以前这条链是断的：prompts.json（05_文案/<套次>/，中文分镜）gen_video.py **根本不读**，
#   出片提示词只由上面的英文骨架 + ip.json 的 prompt_vars 拼成。
#   人在界面里把第 3 段改成什么样，出片都一模一样 —— 界面还假装那是依据。
#
# ⚠ 为什么不直接把中文分镜塞进骨架（2026-10-27 S04 实测，这条是硬约束）：
#   不在 <d> 标记里的中文会被模型当成「画面上该出现的字」，全片烧出字幕，
#   且低分辨率试片看不出来。所以中间必须有 prompts.en.json 这层英文转写，
#   并且这里再查一遍：**任何一个汉字都不许进提示词**。
#
# 采纳条件只有一个：PROMPTS_EN 指到的文件存在（vstudio prompts <IP> <套> --to-en 产出）。
#   没有它就完全走老路 —— 跟改之前逐字节一致，不会出现「不知道为什么变了」。
def _load_seg_en():
    p = (os.environ.get("PROMPTS_EN") or "").strip()
    if not p or not os.path.exists(p):
        return {}
    try:
        d = json.loads(open(p, encoding="utf-8").read())
    except Exception as ex:
        sys.exit(f"✗ prompts.en.json 读不了：{p}\n  → {ex}")
    out = {}
    for s in d.get("segments") or []:
        try:
            i = int(s.get("i"))
        except (TypeError, ValueError):
            continue
        row = {}
        for k in ("expression_en", "perf_en"):
            v = str(s.get(k) or "").strip()
            if not v:
                continue
            cjk = re.findall(r"[\u4e00-\u9fff]", v)
            if cjk:
                # 宁可不出片也不出「烧了字幕的片」——那玩意交出去是要挨骂的
                sys.exit(f"✗ prompts.en.json 第 {i} 段的 {k} 里混进了中文：{''.join(cjk)[:30]}\n"
                         f"  中文进提示词会被模型当成上屏字幕（S04 实测），已在出片前拦停。\n"
                         f"  修：删掉 {p} 重跑 vstudio prompts <IP> <套次> --to-en --force")
            row[k] = v
        if row:
            out[i] = row
    if out and min(out) < 1:
        # 段号基准是 1（人写给人看的）。0 起会让整份分镜错位一段，
        # 而错位的画面「看起来也像在讲」，最难查 —— 宁可不出片。
        sys.exit(f"✗ prompts.en.json 的段号 i 必须从 1 开始（当前最小是 {min(out)}）\n"
                 f"  → {p}\n"
                 f"  修：vstudio prompts <IP> <套次> --to-en --force 重新生成一遍")
    if out:
        print(f"  提示词闭环：采纳 {os.path.basename(p)} 的 {len(out)} 段英文分镜")
        _warn_stale(p, d)
    return out


def _cn_src_hash(path):
    """跟 bin/prompts.py 的 source_hash 同一套算法：中文源 required 字段的 md5。

    ⚠ 这里刻意重复了那边的算法（两边改一处另一边就判不准）。
      宁可重复也不引入依赖：gen_video.py 必须能在只有 ip.json 的机器上独立跑。
    """
    try:
        cn = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return None
    core = [{"camera": s.get("camera", ""), "expression": s.get("expression", ""),
             "i": s.get("i"), "motion": s.get("motion", "")}
            for s in (cn.get("segments") or [])]
    import hashlib
    return hashlib.md5(
        json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _warn_stale(en_path_, en_doc):
    """英文稿跟当前中文稿对不上时报警 —— 这是最难查的一类假片。

    场景：人在工作台改了一段中文，忘了 --to-en，出片照出，
          画面用的是旧分镜，而人以为自己改的那版生效了。
    """
    cn_path = os.path.join(os.path.dirname(en_path_), "prompts.json")
    if not os.path.exists(cn_path):
        return
    now = _cn_src_hash(cn_path)
    old = en_doc.get("source_hash")
    if not now or not old or now == old:
        return
    print("  ⚠⚠ 提示词闭环：prompts.en.json 跟当前中文稿不同源 ——"
          "出片会用**旧的那版英文分镜**")
    print(f"     中文稿改过没重译。修：vstudio prompts <IP> <套次> --to-en --force")
    print(f"     （英文稿记的源指纹 {str(old)[:8]}… vs 当前中文稿 {now[:8]}…）")


SEG_EN = _load_seg_en()


def build_prompt(i, segs, cues, audio_sec):
    start, end = segs[i]
    # 本段若有英文分镜（prompts.en.json），就用它覆盖默认的表情/手势描述。
    # 没有 en 时 pv 就是 PV 本身 —— 走的是改之前那条完全相同的路。
    #
    # ★★ i 是 0 起的循环下标，prompts.en.json 的 "i" 是 1 起的段号（人写给人看的），
    #   这里必须 +1。2026-10-30 实跑抓到：原来写 SEG_EN.get(i)，导致全是错位一段 ——
    #   段1 取不到分镜（静默用默认描述）、段2 用了段1 的分镜、最后一段的分镜永远用不上。
    #   表现是「画面跟台词差半句」，日志一片绿，极难查。
    en = SEG_EN.get(i + 1) or {}
    pv = dict(PV) if en else PV
    if en:
        pv["expression"] = en.get("expression_en") or PV["expression"]
        pv["seg_perf"] = en.get("perf_en") or ""
    t0, t1 = start / FPS, end / FPS
    ov = segs[i - 1][1] - start if i else 0
    new_lo, new_hi = t0 + ov / FPS, t1

    subj = (
        "subject_definitions:\n"
        "<Subject 1> is $who_lc, taken one-to-one from <Picture 1>: $appearance. His body proportions, $posture "
        "and his shoulder line also follow <Picture 1>. $skin "
        # ★ 眉部必须显式写「放松」，否则模型按「认真讲解」的默认表情演，眉间会皱起来。
        #   2026-10-27 用户指出 S04 眉毛皱着。此前提示词里**完全没有眉毛相关描述**，
        #   等于把表情交给了模型默认值。只写「放松」不够，要连眉骨到眉尾的走向一起说，
        #   否则模型会把「放松」理解成「眉毛下垂」。
        "$brows "
        "$expression <Picture 1> is "
        "authoritative for his identity, "
        "his outfit, the room behind him and the quality of the light.\n"
        "<Subject 2> is $scene_role: $scene_objects.\n"
        "<Audio 1> is the locked original $lang voice of <Subject 1>, and the on-screen lip movement follows "
        "it exactly; this audio is used unchanged as the final soundtrack.\n"
    )

    if i == 0:
        cam = (
            "The camera is on a tripod at his eye level. During the first two and a half seconds it eases back "
            "very slightly, a shift of roughly two percent of the frame, so gentle it is felt rather than seen; "
            "after 00:02.500 the camera is locked and stays perfectly still, with no further push, pull, pan, "
            "tilt, zoom or handheld drift for the rest of the take."
        )
    else:
        cam = (
            "The camera is locked off on a tripod at his eye level and holds one fixed frame: the scale, the "
            "height and the angle are identical at the first frame and at the last, with no push, pull, pan, "
            "tilt, zoom or handheld drift."
        )

    # ★★ 2026-10-27 修 bug：summary 原来把**第一条片子的主题写死**在这里
    #   （"telling shop owners that the shop next door is already posting video every day..."），
    #   导致之后每一条新片都被灌进 S03 的语义。这类错不会报错、也不是随机伪影，
    #   它会稳定地把模型往「隔壁店发视频」那个场景带，而画面/口型看着都正常，
    #   极难在验收阶段发现。
    #   现在改为取**本段真正的台词**：把 beat 里的字幕原文拼成语义提纲。
    #   summary 的作用只是给模型一句「这段在讲什么」，不需要文学性，
    #   用原文反而更准 —— 模型能直接对上 <d> 标记里的中文。
    #   ⚠️ 必须放在 beats 计算**之后**（依赖它），见下方 summ 的组装。

    ret = (
        "\nretention_analysis:\n"
        "<Picture 1>: fully_preserved - $ret_objects are carried "
        "over exactly as in <Picture 1>, and the shot keeps one steady medium framing with the same distance from "
        "his face from the first frame to the last.\n"
        "<Audio 1>: fully_copy - the source voice is reused one to one as the final soundtrack and the lip "
        "movement follows it exactly.\n"
    )

    frame_desc = (
        "A steady medium shot of the presenter $seated_where, framed at the middle of his chest so "
        "that his head, both shoulders and both upper arms sit comfortably inside the frame, with $above_head "
        "and a little of the $scene_noun on either side of him; $room_right and $room_left"
    )

    # ★ 姿态来自 ip.json（默认中性）：以前写死 "in his chair ... near his lap"，
    #   站着讲的 IP 会被要求「坐在椅子里、手放在腿上」，和静帧的站姿打架。
    opening = ("He is $rest_pose, his gaze level on the lens, breathing calmly. ")
    if i > 0:
        od = ov / FPS
        opening = (
            f"The opening 00:00.000-00:{od:06.3f} is a carried latent continuation from the preceding "
            "segment: those frames are the preceding segment's own final shot, keeping his exact head position, "
            "hand position, shoulder line, framing, exposure, colour and room tone, before any new action begins. "
            + opening
        )

    # 句子归属：自动按"主要落在本段新增区"判定
    beats = []
    for k, (c0, c1, txt) in enumerate(cues):
        nxt = cues[k + 1][0] if k + 1 < len(cues) else audio_sec
        lo, hi = max(c0, new_lo), min(nxt, new_hi)
        if hi - lo > 0.4:
            beats.append((txt, max(c0, new_lo + 0.05), k))

    # ★ summary 用本段真实台词生成（修掉「写死 S03 主题」的 bug，见上方注释）
    seg_lines = [txt for txt, _t, _k in beats]
    # ★ summary 只描述「在说什么主题」，**不许出现中文字**。
    #   2026-10-27 踩过：为了让 summary 跟上本段真实台词，我把中文原文拼了进去，
    #   结果 H3 把 summary 里的中文当成「画面上该出现的字」，
    #   每一段画面里都烧了对应台词的字幕（S04 全片实测）。
    #   而 <d>[Chinese]…</d> 是**说话**标记，模型没混，混的是 summary。
    #   所以：summary 用英文转述主题，台词只走 <d> 标记。
    #   seg_topic 是一句英文概括，由 seg_lines 的字数/位置推出来，不是原文。
    if seg_lines:
        n_char = sum(len(re.sub(r"[，。？！、；：\s]", "", s)) for s in seg_lines)
        topic = f"about {n_char} characters of $lang across {len(seg_lines)} short phrases"
        summ_body = (f"$who_cap $pose_verb $where_of_still of the reference still and speaks "
                     f"directly into the lens in $lang, delivering {topic}. "
                     f"He is giving a talking-head explainer to camera; no other action, no cutaway.")
    else:
        # 整段都在重叠区（只承接上一段尾部，没有新增台词）
        summ_body = ("$who_cap sits $where_of_still of the reference still and holds the "
                     "same framing while the locked voice carries the line over from the preceding segment.")
    summ = (
        "\nsummary:\n"
        f"{summ_body} "
        f"{cam} The light on his face is broad and diffused, sitting a little below full brightness so the "
        "highlights rest in the upper mid-tones instead of clipping to white; clean open shadow detail, warm "
        "neutral colour temperature, quiet and calm throughout.\n"
        # ★★ 干净底片：只描述画面里**有什么**，一个文字类词都不提。
        #
        #   2026-10-27 实测记录（四种写法全部**未能**阻止字幕，别指望提示词解决）：
        #     ① `no_onscreen_text: no subtitles / no captions…`  → 有字幕
        #     ② 改成 clean_plate 但含 lettering / caption / overlay → 有字幕
        #     ③ 去掉所有文字类词，只描述墙/植物/灯/衬衫       → **还是有字幕**
        #     ④ 加 (S1) 说话人 ID + `speaks:`                 → **还是有字幕**
        #
        #   反证：同一套提示词、同一台机器、同样 864x1536，跑 S03 的 SRT 和音频
        #   → **画面干净**。所以不是提示词写法的问题，是**台词内容**触发的：
        #   S04 首句「那二十分钟我到底做了什么」是标题式提问，S03 首句
        #   「你隔壁那家店」是场景描述。模型把前者当成该上屏的标题。
        #
        #   字幕证据：内容是 <d> 原文逐字拷贝（把「到底」改成「都」，字幕同步变），
        #   只把「二十分钟」规范成「20分钟」。分辨率相关：0.25MP 下糊到看不清，
        #   1.26MP 就显出来 —— 所以**低分辨率试片验不出这个问题**。
        #
        #   结论：这段描述留着（正面描述画面是对的，符合人设圣经 §13 禁否定词），
        #   但**它挡不住字幕**。真正的防线是「出片后立刻抽帧看」，见 SKILL.md §3。
        "clean_plate: the frame is pure live-action footage of the room. "
        "$wall_prop fills the "
        "space above his hair and continues on both sides of him, $edge_right "
        "and $edge_left, and the lower part of the frame carries $torso. "
        "The room and the fabric read exactly as they do in the reference still, wall and cloth filling "
        "every part of the frame that is not his face.\n"
    )

    acts = []
    for txt, t, k in beats:
        g = (CALM_GESTURES if i >= 3 else GESTURES)[k % len(GESTURES)]
        # ★ "near his lap" 是坐姿假设的参照点；站着讲的 IP 在 ip.json 里给 lap_ref，
        #   否则默认手势会把「手在腿边」硬塞给一个站立的人。
        _lap = PV.get("lap_ref") or "his lap"
        if _lap != "his lap":
            g = g.replace("his lap", _lap)
        # (S1) 说话人 ID：H3 规范 §4.4 要求「说话人标识放在 <d> 外面」，这样写合规。
        # ⚠️ 但要说清楚：**它并不能阻止字幕被画进画面**（2026-10-27 实测无效）。
        #    详见 SKILL.md §3「出片后必须先抽帧看画面有没有字」。
        acts.append(f"At {ts(max(0.0, t - t0))} the presenter (S1) speaks: "
                    f"<d>[$lang_tag]{txt}</d> {g}. ")
    if not acts:
        acts.append("He speaks the line carried in on the locked voice while holding the same steady framing. ")

    # ★ 2026-10-28：第 4 段起（i>=3）表情收敛 —— 压住大笑 / 挑眉带来的
    #   法令纹 / 抬头纹加深，让脸保持平静、嘴部小幅、眉不挑。
    #
    #   ⚠️ 2026-10-28 S05 首跑即失败：原写法 "with no broad smiles and no raised brows"
    #   撞了本 IP 的 negative_free 红线（ip.json rules，2026-10-27 用户定板），
    #   被 DRY 的否定词 assert 拦下。这段是 S04 出片之后才加的，所以**从未被真跑验证过**。
    #   已改为正向描述（语义等价：嘴只小幅、眉平放），不得再写回 no/not/never/without。
    calm_expr = (
        "His facial expression stays calm and contained throughout this segment: he speaks with a relaxed, "
        "even face and only small, controlled mouth movement, his mouth holding a faint closed-lip smile and "
        "his brows resting level and settled in their natural position, keeping his forehead smooth and the "
        "lines beside his nose soft and uncreased. "
        if i >= 3 else ""
    )
    # perf 是本段的**手势/动作**从句，来自 prompts.en.json。
    # 位置：紧接着 opening（坐姿与呼吸），在第一个 <d> 说话标记之前 ——
    #   模型先知道「人怎么坐着」，再知道「手在做什么」，最后才对口型讲台词。
    # 没有 prompts.en.json 时它是空串，拼出来跟以前一字不差。
    perf = pv.get("seg_perf") or ""
    if perf and not perf.endswith(" "):
        perf += " "
    act = opening + perf + "".join(acts) + (
        "Between phrases his head makes small natural tilts with the rhythm of his speech, his shoulders stay "
        "settled and his hands return to rest after each gesture. " + calm_expr +
        "He is the only person in the room."
    )

    style = (
        "Realistic vertical 9:16 talking-head video, one large diffused key light close to his face giving broad "
        "gradual falloff, exposure held a little under full so every lit area of his face sits in the upper "
        "mid-tones, clean open shadow detail, warm neutral colour temperature, shallow depth of field with his "
        "face in crisp focus, natural dry matte skin with visible pores and an even diffuse sheen of a single "
        "flat value across forehead, nose and cheeks with the forehead matching the cheeks in tone, tones "
        "consistent with the reference still."
    )

    hold = (
        "His face keeps its exact identity and bone structure in every frame, his hairstyle keeps the same shape "
        "and volume, $held_items. The $scene_noun behind him keeps $bg_kept; "
        "the light direction, the colour temperature, the exposure and the "
        "overall brightness stay constant for the whole take, and the soft sheen on his forehead holds the same "
        "value from the first frame to the last, staying flat and even as the take goes on."
    )

    det = (
        "\ndetailed_description:\n"
        f"{style} [Shot 1] One continuous take, {frame_desc}. {act} {hold}\n"
    )

    snd = (
        "\noverall_soundscape:\n"
        "$ambience, his clear close $lang voice "
        "sitting in the foreground, and nobody else present.\n"
    )

    mus = (
        "\nnon_diegetic_music:\n"
        "$music\n"
    )
    # ★ 变量统一在这里替换（不在各处拼的时候替换），好处是拼装逻辑保持纯拼接、
    #   一眼能看出最终结构，改业务不用在一个个 f-string 里找。
    return _gender_norm(Template(subj + summ + ret + det + snd + mus).safe_substitute(pv), GENDER)


def _gender_norm(text, g):
    """把骨架里写死的 he/his 按本 IP 的性别归一。

    ★ 为什么要有：这套英文骨架是全引擎共用的一份，里面 90 处 he/his 是「男性默认」。
      女性 IP（客户女主播）直接用，出来的提示词就是「一位女性……他的脸……他坐在椅子上」，
      模型会摇摆，画面容易中性化。这跟「引擎默认值必须中性」是同一条铁律。

    ★ 只在 video.gender 明确是女性时替换 —— 主号（male）走同一行代码但逐字节不变，
      不重新验证主号的历史成片。\b 词边界是必须的：'the' 里含 he，
      裸子串替换会把它改成 'tshe'（这类错误比不替换更难查）。
    """
    if str(g).lower() not in ("f", "female", "女", "女性", "woman"):
        return text
    for a_, b_ in (("himself", "herself"), ("his", "her"), ("His", "Her"),
                   ("him", "her"), ("Him", "Her"), ("he", "she"), ("He", "She")):
        text = re.sub(r"\b%s\b" % a_, b_, text)
    return text


def timeline(segs, cues, audio_sec, total_sec, offset=0, full=None, frame_base=0):
    """offset = 这批 segs 在全片里的起始段号（0 起）；full = 全片分段表；
    frame_base = 本批第一段的绝对起始帧（PILOT 只跑中段时用来平移）。

    ★ 为什么需要这三个：
    ① offset：PILOT=3 只把第 3 段切成 `segs[-1:]` 交给这里，如果下标从 0 重新数，
       `i==0` 就会命中「段1 的缓推镜头」，素材也取段1那张 —— 「只测第 3 段」
       测出来的是「段1的提示词 + 段3的时间轴」，等着看末段劣化永远等不到。
       （英文分镜按段号取，键是 1 起的，见 build_prompt 里的 +1。）
    ② frame_base：插件硬校验「第 1 段必须从第 0 帧开始、段间无缝且重叠对齐」
       （minimax_h3_finite_segments.py:461）。不平移的话 PILOT=2 直接被拒：
       「Segment 1 must advance in time without gaps」—— 这个功能以前就是坏的。
       平移后起始帧归 0，而音频用 trimStart 取对应的那一段，时间关系不变。
    """
    full = segs if full is None else full
    sel_dur = (max(e for _s, e in segs) - frame_base) / FPS
    # 素材箱：起始静帧 + materials.json 里登记的额外图。
    # 顺序无所谓，真正决定「某段用哪几张」的是下面 segmentConfig 里的 images（按 id 取）。
    images = [{"id": BASE_IMAGE_ID, "file": f"{REL}/{STILL_NAME}", "name": STILL_NAME,
               "width": 1088, "height": 1920}]
    for m in EXTRA_IMAGES:
        images.append({"id": m["id"], "file": f"{REL}/{m['in_name']}",
                       "name": m["name"], "width": m["w"], "height": m["h"]})

    # ---- 参考视频 ----
    # ★ 定位方式跟图片不一样：图片是「段里点名用哪几张」（按 id），
    #   视频是「按时间区间摆位置」—— videoClips 自己带 start/duration，
    #   插件拿它跟 selection 求交集，交集部分才是生效的部分。
    #   所以这里把视频的时间窗**对齐到它所属的那一段**，段多长它就多长。
    #
    #   referenceMode 三种（源码 _reference_mode 只认这三个）：
    #     guide    硬锁重叠区 → 续写、保持运动
    #     edit     只作 <Video N> 参考，不硬锁原人物 → 换人 / 换服装 / 换风格
    #     boundary 只锁重叠区首尾帧
    vclips = []
    for k, (s, e) in enumerate(segs):
        idx = offset + k + 1                      # 绝对段号（1 起）
        for vid in (SEG_VIDEOS.get(idx) or []):
            v = VIDEO_BY_ID.get(vid)
            if not v:
                continue
            vclips.append({
                "id": v["id"], "file": f"{REL}/{v['in_name']}", "name": v["in_name"],
                "start": round(s / FPS, 4),
                "duration": round((e - s) / FPS, 4),
                "trimStart": v["trim_start"],
                "referenceMode": v["use"],
            })
    if vclips:
        for c in vclips:
            print(f"   参考视频 {c['id']}（{c['referenceMode']}）"
                  f" {c['start']}s 起、{c['duration']}s")
        # ★ 2026-10-30 实测：拿一条**已经烧好字幕**的成片当参考视频，模型会照着
        #   参考画面自己在片子里「画」字（位置还不一定对，可能跑到画面顶部），
        #   再叠一层正式字幕 → 双层重影。这不是代码错，是素材选错，但片子交出去就是废的。
        print("   ⚠ 参考视频请用**无字幕的原始素材**：模型会照着参考画面自己生成文字，"
              "带字的素材会跟正式字幕叠成重影（水印/台标同理）")

    return {
        "version": 10, "fps": FPS, "globalPrompt": "",
        "secondPass": SECOND_PASS,
        "secondPassModel": UPSCALER, "secondPassHighSteps": SECOND_PASS_STEPS,
        "selection": {"start": 0, "duration": sel_dur},
        "videoAudioEnabled": True, "videoClips": vclips,
        "images": images,
        "audios": [{"id": "aud", "file": f"{REL}/{AUDIO_NAME}", "name": AUDIO_NAME,
                    "duration": sel_dur if frame_base else audio_sec,
                    "trimStart": frame_base / FPS, "audioMode": "locked"}],
        "segmentConfig": {
            "count": len(segs), "activeIndex": 0, "mode": "timeline",
            # ★ 每段都显式列 images。留空不是「用全部」，是「这段没有参考图」→ 脸崩。
            #   插件会按段重新筛一遍并按顺序重新编号 <Picture N>，所以骨架里写死的
            #   <Picture 1> 在第 2 段自动指向新素材 —— 英文模板一个字都不用改。
            "segments": [{"images": seg_image_ids(offset + k + 1), "audios": ["aud"],
                          "prompt": build_prompt(offset + k, full, cues, audio_sec),
                          "startFrame": s - frame_base, "endFrame": e - frame_base}
                         for k, (s, e) in enumerate(segs)],
        },
    }


def graph(prefix, segs, cues, audio_sec, total_sec, offset=0, full=None, frame_base=0):
    hc, qc, ts = lowvram_settings()
    use_patch = hc > 0 or qc > 0

    g = {
        "82": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "63": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "minimax", "device": "default"}},
        "65": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "66": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "47": {"class_type": "ResolutionSelector", "inputs": {"aspect_ratio": "9:16 (Portrait Widescreen)",
                                                             "megapixels": MP, "multiple": MULTIPLE}},
        "13": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "11": {"class_type": "MiniMaxH3TimelinePlanner", "inputs": {
            "width": ["47", 0], "height": ["47", 1], "generation_seconds": total_sec,
            "timeline_data": json.dumps(timeline(segs, cues, audio_sec, total_sec,
                             offset=offset, full=full, frame_base=frame_base),
                             ensure_ascii=False)}},
    }

    # ★ model 链：UNET → LoRA → Sage → 低显存分块 → 消费者
    #   顺序是有讲究的，不是随便串：
    #     ① LoRA 必须紧跟 UNETLoader —— 它改的是权重；
    #     ② Sage 在 LoRA 之后 —— 它改的是 attention 实现，跟权重无关，但必须
    #        作用在已经合过 LoRA 的模型上；
    #     ③ 低显存分块放**最外层** —— 它才是逐块切 attention 的那个，套在 Sage
    #        外面才能把 Sage 的 attention 也切片；反过来套就会被 Sage 覆盖，
    #        分块白挂、显存照样爆。
    #   ★ 关键：BasicScheduler 和 Sampler 必须吃**同一个** m_src。只给 Sampler
    #     挂而 Scheduler 吃原始模型，两边用的不是同一个对象，补丁等于没生效
    #     —— 2026-10-27 就栽在这，表现是「加了 8G 省显存的节点，显存一点没降」。
    m_src = ["82", 0]
    if LORA and LORA != "none":
        g["84"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": m_src, "lora_name": LORA,
                              "strength_model": LORA_STRENGTH}}
        m_src = ["84", 0]
    if SAGE and SAGE != "disabled":
        g["85"] = {"class_type": "PathchSageAttentionKJ",
                   "inputs": {"model": m_src, "sage_attention": SAGE,
                              "allow_compile": False}}
        m_src = ["85", 0]
    if use_patch:
        g["83"] = {"class_type": "WASH3LowVRAM", "inputs": {
            "model": m_src, "head_chunks": hc, "query_chunks": qc, "token_slice": ts}}
        m_src = ["83", 0]

    g["9"] = {"class_type": "BasicScheduler", "inputs": {"model": m_src, "scheduler": "simple",
                                                         "steps": STEPS, "denoise": 1.0}}
    g["30"] = {"class_type": "MiniMaxH3FiniteSegmentVideoSampler", "inputs": {
        "model": m_src, "clip": ["63", 0], "vae": ["65", 0], "audio_vae": ["66", 0],
        "finite_plan": ["11", 2], "sampler": ["13", 0], "sigmas": ["9", 0],
        "seed": SEED, "continue_audio_latent": True, "ref_image_size": "match"}}
    g["29"] = {"class_type": "SaveVideo", "inputs": {"video": ["30", 1], "filename_prefix": f"video/{prefix}",
                                                     "format": "mp4", "codec": "h264"}}

    # ★ 工作流弹性层（2026-10-28 用户提「以后要做参考视频换人/换背景/换台词」）
    #   思路：H3 这条链本质是「参考图 + 锁定的音频 + 时间轴」驱动，
    #   换人=换参考图/换 head_swap LoRA，换背景=换参考图，换台词=换 TTS 音频，
    #   都是**改参数**而不是改代码。所以把节点参数开放成档案卡可覆盖：
    #     ip.json → video.graph_overrides = {"节点id": {"输入名": 值, ...}}
    #   引擎只负责合并，不认识具体业务。以后接新工作流（换背景/换人专用图）
    #   也不用改 gen_video.py，改档案卡即可 —— 这就是「引擎只放算法、
    #   IP 只放参数」在 ComfyUI 侧的落地。
    ov = IP.get("video.graph_overrides") or {}
    for nid, patch in ov.items():
        nid = str(nid)
        if nid in g and isinstance(patch, dict):
            g[nid]["inputs"].update(patch)
            print(f"    ↳ graph_overrides 覆盖节点 {nid}：{list(patch)}")
        else:
            print(f"    ⚠ graph_overrides 里的节点 {nid} 不存在，已忽略")
    return g


def post(data):
    req = urllib.request.Request(COMFY + "/prompt", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        sys.exit(f"✗ ComfyUI 拒收这条出片工作流（HTTP {ex.code}）：\n"
                 + ex.read().decode("utf-8", "ignore")[:1200])
    except Exception as ex:
        sys.exit(f"✗ 提交出片失败：连不上 ComfyUI（{COMFY}）\n"
                 f"  {type(ex).__name__}: {ex}\n"
                 f"  先确认它还在跑：curl -s --noproxy '*' {COMFY}/system_stats")


def poll(pid, timeout=10800):
    """等出片结果。

    ★ ComfyUI 挂了必须报错，不能死等 3 小时。2026-10-30 实测：TTS 阶段服务崩掉
      （内存还有 58G，不是 OOM），轮询永不返回，批量任务静默卡了 12 分钟一个字不打。
      出片本身可能跑很久，所以阈值放宽到「连续 100 秒连不上」才判死。
    """
    end = time.time() + timeout
    t0 = time.time()
    bad = 0
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=30) as r:
                h = json.loads(r.read().decode())
            bad = 0
        except Exception as ex:
            bad += 1
            if bad >= 10:
                sys.exit(f"✗ 连不上 ComfyUI（{COMFY}）：{type(ex).__name__}: {ex}\n"
                         f"  任务 {pid} 拿不到结果，已停止等待。\n"
                         f"  先确认还在跑：curl -s --noproxy '*' {COMFY}/system_stats\n"
                         f"  出片是整片一次提交、盘上不留半成品，服务恢复后要整段重出：\n"
                         f"    vstudio pipeline <IP> <套次> --from video")
            time.sleep(10)
            continue
        if pid in h:
            return h[pid]
        waited = int(time.time() - t0)
        if waited >= 300 and waited % 300 < 16:
            print(f"  … 已等 {waited // 60} 分钟（出片正常 2–6 分钟/段，"
                  f"超 20 分钟请看 ComfyUI 日志）")
        time.sleep(15)
    return None


def md5(p):
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def prep(audio_sec):
    assert os.path.exists(STILL_SRC), STILL_SRC
    assert os.path.exists(AUDIO_SRC), AUDIO_SRC
    # ★ 先探活再干活。顺序有讲究：ComfyUI 死着的时候，下面那步
    #   「音频 →input」会白做一遍，然后才在提交时抛一个看不懂的 502 堆栈。
    #   2026-10-27 就因为这个多绕了十几分钟才定位到「进程被 OOM 杀了」。
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=8) as r:
            r.read()
    except Exception:
        sys.exit(f"✗ 连不上 ComfyUI（{COMFY}）\n"
                 f"  十有八九是进程已经被内核 oom-killer 杀了。\n"
                 f"  查证：journalctl -k --since '-30min' | grep -i 'out of memory'\n"
                 f"  重启：cd /cfu/ComfyUI && ./run.sh\n"
                 f"  （重启前先看 comfy_free.py 顶部那段，搞清为什么会 OOM）")
    os.makedirs(IN_DIR, exist_ok=True)
    d = os.path.join(IN_DIR, STILL_NAME)
    if not os.path.exists(d) or md5(d) != md5(STILL_SRC):
        shutil.copy2(STILL_SRC, d)
        print("静帧 →input")
    # 分段素材：额外那几张也要进 ComfyUI 的 input 目录，否则图里的 file 指向空
    for m in EXTRA_IMAGES:
        dst = os.path.join(IN_DIR, m["in_name"])
        if not os.path.exists(dst) or md5(dst) != md5(m["src"]):
            shutil.copy2(m["src"], dst)
        print(f"素材 {m['id']}（{m['name']}） →input")
    # 参考视频同理：不进 input，时间轴里那个 file 就指向一个不存在的路径，
    # 插件会静默跳过（不报错、也不生效）——「我明明挂了为什么没变化」就是这么来的。
    for v in EXTRA_VIDEOS:
        dst = os.path.join(IN_DIR, v["in_name"])
        if not os.path.exists(dst) or md5(dst) != md5(v["src"]):
            shutil.copy2(v["src"], dst)
        print(f"参考视频 {v['id']}（{v['name']}，{v['use']}） →input")
    a = os.path.join(IN_DIR, AUDIO_NAME)
    subprocess.run([FF, "-nostdin", "-y", "-i", AUDIO_SRC, "-af", "apad=pad_dur=0.60",
                    "-t", f"{audio_sec}", "-ar", "48000", "-ac", "2", "-b:a", "192k", a],
                   check=True, capture_output=True)
    print("音频 →input")


def main():
    assert SRT and AUDIO_SRC, "必须给 SRT= 和 AUDIO= 环境变量"
    cues = parse_srt(SRT)
    audio_sec = audio_dur(AUDIO_SRC)
    # 总帧数要落在 5+17n 网格上，且不短于音频
    need = int(audio_sec * FPS) + 1
    total = snap(need, lo=need, hi=3600)
    total = 5 + 17 * ((need - 5 + 16) // 17)
    total_sec = total / FPS

    # 段数自适应：默认目标段数排出来的切点若有落在半句上的，就近换个段数重排。
    # 为什么必须做：OVERLAP 从 22 改 5 后 DP 的均分目标变了，段数不变时切点会平移。
    # S03 实测默认 5 段 → 帧 187 切在半句上；改 6 段 → 5/5 全落在停顿。
    # 用 SEG_FIXED=1 可关掉自适应（手动锁定 TARGET_SEGS）。
    # ★ SEG_OVERRIDE 优先级最高：手动指定分段，完全不做均分。
    #   自动分段是「等长均分 + 切点吸向停顿」，遇到「这段要长、那段要短」的业务需求
    #   就无解 —— 它会为了长度接近而牺牲切点位置。手动模式直接按你给的表走。
    _ovr = os.environ.get("SEG_OVERRIDE", "").strip()
    _durs = os.environ.get("SEG_DURS", "").strip()
    _grid = os.environ.get("SEG_GRID", "").strip()
    if _grid:
        # ★ SEG_GRID 优先级最高：它是唯一「一步算出切点」的写法，
        #   不存在吸附不准或切点落不到位的问题（切点天生就是 17 的倍数）。
        try:
            segs, _n = parse_seg_grid(_grid, total)
        except ValueError as e:
            sys.exit(f"✗ {e}")
        _cum, _acc = [], 0
        for x in _n[:-1]:
            _acc += x
            _cum.append(_acc * 17)
        print(f"   分段来源 SEG_GRID（按 17 格数直接算切点）  格数 "
              f"{'/'.join(str(x) for x in _n)}  共 {sum(_n)} 格")
        print(f"   切点 = 17×累加格数 = {[c for c in _cum]}"
              f"  = {[round(c/FPS, 2) for c in _cum]}s")
        tot = validate(segs)
    elif _ovr:
        try:
            segs = parse_seg_override(_ovr, total)
        except ValueError as e:
            sys.exit(f"✗ {e}")
        print(f"   分段来源 SEG_OVERRIDE（手动精确，不做均分）")
        tot = validate(segs)
    elif _durs:
        try:
            ds = [float(x) for x in _durs.split(",") if x.strip()]
            segs = snap_seg_override(ds, total, cues)
        except ValueError as e:
            sys.exit(f"✗ {e}")
        print(f"   分段来源 SEG_DURS（按目标秒数吸附，不做均分）"
              f"  目标 {'/'.join('%gs' % d for d in ds)}")
        tot = validate(segs)
    elif not os.environ.get("SEG_FIXED"):
        best = None
        # 搜索范围 ±4：1.33MP 下 S03 的最优解是 TARGET 5→8（delta=+3），
        # 只搜 ±2 会漏掉。段数再往上加对成片没好处（段越短越容易露馅），故封 ±4。
        for delta in (0, 1, -1, 2, -2, 3, -3, 4, -4):
            cand_n = TARGET_SEGS + delta
            if cand_n < 2:
                continue
            try:
                cand = plan_segments(total, cues, cand_n)
                validate(cand)
            except Exception:
                continue
            hit, nn, bad = cut_quality(cand, cues)
            score = (0 if not bad else 1, len(bad), abs(delta), len(cand))
            if best is None or score < best[0]:
                best = (score, cand, cand_n)
            if not bad:
                break
        if best and best[2] != TARGET_SEGS:
            print(f"   段数自适应：TARGET_SEGS {TARGET_SEGS} → {best[2]}（原段数会让切点落在半句上）")
        segs = best[1] if best else plan_segments(total, cues, TARGET_SEGS)
        tot = validate(segs)
    else:
        segs = plan_segments(total, cues, TARGET_SEGS)
        tot = validate(segs)

    print(f"{TAG}: 音频 {audio_sec:.2f}s → 输出 {total} 帧 / {total_sec:.3f}s / {len(segs)} 段")
    # ★★ 英文分镜段数必须与实际出片段数一致，不一致就明确报警。
    #   以前是 `SEG_EN.get(i) or {}` —— 少了的段静默退回默认描述，画面跟台词对不上，
    #   日志一片绿，人只会觉得「越到后面越不对」却查不出为什么（2026-10-30 实跑抓到：
    #   出 3 段、只有 2 段提示词，因为提示词是按「45 字」切的，跟出片段数是两套算法）。
    if SEG_EN:
        # 注意基准：SEG_EN 的键是**1 起的段号**（prompts.en.json 的 "i"），
        # segs 是 0 起的下标 —— 两边比之前必须先 +1，否则永远误报。
        _need = set(range(1, len(segs) + 1))
        _missing = sorted(_need - set(SEG_EN))
        _extra = sorted(set(SEG_EN) - _need)
        if _missing or _extra:
            print(f"  ⚠ 英文分镜段数对不上：实际出 {len(segs)} 段，"
                  f"prompts.en.json 有 {len(SEG_EN)} 段")
            if _missing:
                print(f"    第 {','.join(map(str, _missing))} 段没有对应分镜 → 会退回默认描述"
                      f"（画面与台词不符）")
            if _extra:
                print(f"    prompts.en.json 第 {','.join(map(str, _extra))} 段用不上，被忽略")
            print(f"    修：vstudio prompts <IP> <套次> --gen --tier <本档位> "
                  f"然后 --to-en --force")
            print(f"    （段数由档位决定：同一份文案 fast / std 可能差一段）")
    # ★ 每次都必须明确打印「用的是谁的脸」——多 IP 下这是唯一的防混用护栏
    print(f"   静帧 {os.path.basename(STILL_SRC)}")
    if not os.path.isfile(STILL_SRC):
        sys.exit(f"✗ 静帧不存在：{STILL_SRC}\n"
                 f"  多 IP 场景请用 STILL=<本IP静帧.png> 指定")
    _w, _h = calc_res()
    print(f"   净长 {tot} 帧（{tot/FPS:.3f}s）  字幕 {len(cues)} 条")
    # ★ 把「这一版到底怎么分段」落到文件 —— 提示词侧必须按它写分镜。
    #   段数**不是** studio_algo.plan() 直接给的：下面还有一步「按切点是否落在
    #   停顿上」在 ±4 范围内换段数重排（2026-10-30 就是 plan 说 4 段、
    #   这里重排成 5 段，结果提示词只写了 4 段，第 5 段静默退回默认描述）。
    #   所以真值只在这里，写出来让 prompts --gen 读。
    _plan_out = (os.environ.get("PLAN_OUT") or "").strip()
    if _plan_out:
        try:
            with open(_plan_out, "w", encoding="utf-8") as _f:
                json.dump({"set": TAG, "n": len(segs), "total_frames": tot,
                           "total_sec": round(tot / FPS, 3),
                           "audio_sec": round(audio_sec, 3), "mp": MP,
                           "maxlen": MAXLEN, "overlap": OVERLAP,
                           "segs": [list(s) for s in segs],
                           "cuts_sec": [round(s[0] / FPS, 2) for s in segs[1:]]},
                          _f, ensure_ascii=False)
            print(f"   分段计划 → {_plan_out}（{len(segs)} 段，提示词按这个写）")
        except Exception as _ex:
            print(f"  ⚠ 分段计划落盘失败：{_ex}")
    print(f"   分辨率 MP={MP} → {_w}x{_h}（{_w*_h/1024/1024:.3f}MP）"
          f"{'  ← 注意：验收工具 W,H 需同步' if (_w, _h) != (768, 1376) else ''}")
    # ★ 快速档警告必须在 DRY 阶段就打 —— DRY 恰恰是决定跑不跑的那一步，
    #   等到真提交时才警告已经晚了（用户可能就是一版拿来交付的）。
    if FAST:
        print(f"   ⚠ 快速测试档（{MP}MP，约 {MP/_BASE_MP*100:.0f}% 面积）："
              f"只能验提示词/分段/动作/口型/接缝位置，"
              f"❌ 不能验清晰度、曝光、面部细节、皮肤质感。出正式片请去掉 FAST=1。")
    _hc, _qc, _ts = lowvram_settings()
    if _hc or _qc:
        print(f"   低显存分块 head={_hc} query={_qc} token_slice={_ts}（MAXLEN={MAXLEN}）")
    else:
        print(f"   低显存分块 关闭（MAXLEN={MAXLEN}）")
    # SEG_DURS 模式下，逐段标出「你想要的秒数 → 实际排出的秒数」。
    # ★ 必须逐段显示而不是只报一个 max 偏离值：用户说「第1段 8s、第5段 6s」时，
    #   真正要看的是哪几段被让掉了、让了多少，只看总数会以为全按他的意图排。
    _want = [None] * len(segs)
    if _durs:
        _wd = [float(x) for x in _durs.split(",") if x.strip()]
        if len(_wd) == len(segs):
            _want = _wd
    for i, (s, e) in enumerate(segs):
        ov = segs[i - 1][1] - s if i else 0
        line = f"   段{i+1}  {s:4d}-{e:4d}  {e-s:3d}帧 {s/FPS:6.2f}-{e/FPS:6.2f}s  重叠{ov}"
        # 分段素材：每段用哪几张图必须打出来 —— <Picture N> 是段内重新编号的，
        # 不打出来人根本不知道「第2段的 Picture 1 到底是啥」。
        if EXTRA_IMAGES:
            names = []
            for n, iid in enumerate(seg_image_ids(i + 1), 1):
                if iid == BASE_IMAGE_ID:
                    names.append(f"<Picture {n}>=静帧")
                else:
                    m = next((x for x in EXTRA_IMAGES if x["id"] == iid), None)
                    names.append(f"<Picture {n}>={m['name'] if m else iid}")
            line += "  " + " ".join(names)
        if _want[i] is not None:
            real = (e - s) / FPS
            d = real - _want[i]
            mark = "" if abs(d) < 0.12 else f"   ← 目标{_want[i]:g}s，实际{real:.2f}s（{d:+.2f}s）"
            line += mark
        print(line)
    # ★ 接缝质量必须每次都打出来。分段表里的数字"合法"不等于切点"合理"。
    print_cut_quality(segs, cues)

    # ⚠ 这里不能写 os.environ.get("DRY")：字符串 "0" 是真值，
    #   于是 DRY=0 也会走干跑分支，跑完只打印提示词、不出片，还返回 0 ——
    #   看起来像"成功出片了"，实际一个字节都没生成（2026-10-28 栽过）。
    #   四个开关（DRY/PILOT/FAST/...）统一这套判据，别再各写各的。
    if (os.environ.get("DRY") or "").strip().lower() not in ("", "0", "false", "off", "no"):
        for i in range(len(segs)):
            p = build_prompt(i, segs, cues, audio_sec)
            print(f"\n=========== 段 {i+1} 提示词 ===========")
            print(p)
            det = p.split("detailed_description:")[1].split("overall_soundscape:")[0]
            for w in [" no ", " not ", " never ", " without "]:
                assert w not in det.lower(), f"段{i+1} detailed_description 有否定词 {w!r}"
        print("\n✓ 所有约束通过")
        # 铁律 8：改完影响出片的东西，先存 golden 再 diff。
        # 提示词是文本、肉眼可比；timeline JSON 是结构体、肉眼比不了 —— 所以要能 dump。
        #   DRY=1 DUMP_TIMELINE=1 <脚本> > 改后.json   再用旧版本跑一份 diff
        if (os.environ.get("DUMP_TIMELINE") or "").strip() not in ("", "0"):
            print("\n=========== timeline ===========")
            print(json.dumps(timeline(segs, cues, audio_sec, total_sec),
                             ensure_ascii=False, indent=2, sort_keys=True))
        return

    prep(audio_sec)

    # ★★ 提交前内存门禁（2026-10-27 加，起因见 comfy_free.py 的文档）
    #   实测一次 S04 全片提交会让单个 prompt 内部反复 stage
    #   TextEncoder(14.9GB)×7 + H3(20GB)×2 且 `0 models unloaded`，
    #   峰值 RAM 47.9GB → 内核 oom-killer 直接 SIGKILL，**不留 Traceback**。
    #   最坑的是它发生在 20 分钟之后：前面烧掉的 GPU 时间全部作废。
    #   所以宁可在提交前 3 秒拒绝开跑。
    #   门槛取 40GB：留 21GB 给系统/桌面/其他进程，且低于实测峰值死线 48GB。
    #   可用 SKIP_MEMCHECK=1 跳过（例如明知只剩 PILOT 单段、内存吃不满时）。
    if not os.environ.get("SKIP_MEMCHECK"):
        r = subprocess.run([sys.executable, os.path.join(HERE, "comfy_free.py"),
                            "--check", "--need", "40"], capture_output=True)
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        print(out)
        if r.returncode != 0:
            sys.exit("✗ 内存不足，拒绝提交（避免被内核杀掉、白烧 GPU 时间）")

    # PILOT 默认只跑第 1 段。但「越到后面越油腻」这个缺陷只出现在**末段**，
    # 只测第 1 段在结构上就看不见它 —— 必须能直接点名跑第 N 段。
    #   PILOT=1      → 第 1 段
    #   PILOT=last   → 最后一段（验收后段劣化用）
    #   PILOT=3      → 第 3 段
    if not PILOT:
        s = segs
        _off = 0
    else:
        _k = PILOT_SPEC
        if _k in ("", "1", "true", "yes", "on"):
            _off = 0
        elif _k in ("last", "end"):
            _off = len(segs) - 1
        else:
            _off = max(0, min(len(segs) - 1, int(_k) - 1))
        s = segs[_off:_off + 1]
        print(f"   PILOT 指定段 = 第 {_off + 1}/{len(segs)} 段")
    # 本批的绝对起点与总长。全片跑时 _base=0、_batch_sec=total_sec，
    # 与加这段之前逐字节一致；只有 PILOT 才真的平移。
    _base = s[0][0]
    _batch_sec = (s[-1][1] - _base) / FPS
    prefix = f"{TAG}_pilot" if PILOT else f"{TAG}_full"
    # ★ 别写死「二采=V2」：ip.json 的 second_pass.enabled 现在真的会生效，
    #   关掉时还打印「二采=V2」就是在骗人（排查「为什么画质没提升」会查到死）。
    _2p = f"二采开({UPSCALER} · {SECOND_PASS_STEPS} 步)" if SECOND_PASS else "二采关"
    print(f"提交 {len(s)} 段 / seed={SEED} / {_2p}", flush=True)
    t0 = time.time()
    r = None
    for attempt in range(1, 4):
        # ★ offset/full 必须传：只跑单段时，提示词与素材仍要按**全片的段号**取，
        #   否则「只测第3段」拿到的其实是段1的提示词（2026-10-30 修）。
        res = post({"prompt": graph(prefix, s, cues, audio_sec, _batch_sec,
                                    offset=_off, full=segs, frame_base=_base),
                    "client_id": "c_%d" % int(time.time())})
        pid = res["prompt_id"]
        print(f"[{attempt}] submitted {pid}", flush=True)
        r = poll(pid)
        st = (r or {}).get("status", {})
        if st.get("status_str") == "success":
            break
        msgs = st.get("messages", [])
        intr = any(m[0] == "execution_interrupted" for m in msgs if isinstance(m, list))
        err = ""
        for m in msgs:
            if isinstance(m, list) and m[0] == "execution_error":
                err = str(m[1].get("exception_message"))[:300]
        print(f"[{attempt}] 失败{'（外部中断）' if intr else ''}: {err}", flush=True)
        if not intr:
            sys.exit(1)
        time.sleep(20)
    if not r or r.get("status", {}).get("status_str") != "success":
        sys.exit(1)
    print(f"生成完成 {time.time()-t0:.0f}s", flush=True)

    fs = glob.glob(os.path.join(COMFY_OUT, "video", f"{prefix}_*.mp4"))
    src = max(fs, key=os.path.getmtime)
    tag = "pilot" if PILOT else "full"
    # ★ 快速档产物必须自带 _FAST 标记，否则低分辨率样片会被误当成品交付。
    fmark = "_FAST%smp" % (("%g" % MP).replace(".", "")) if FAST else ""
    out = os.path.join(HERE, f"{TAG}_raw_{tag}{fmark}.mp4")
    shutil.copy2(src, out)
    print("OK ->", out, os.path.getsize(out), flush=True)
    if FAST:
        print(f"⚠ 这是快速测试档（{MP}MP）——只能验提示词/分段/动作/口型，"
              f"不能拿来验清晰度和曝光。出正式片请去掉 FAST=1。", flush=True)


if __name__ == "__main__":
    main()
