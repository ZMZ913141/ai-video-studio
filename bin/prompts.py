#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompts —— 提示词工作台（引擎侧）

落盘位置：**档案侧** `05_文案/<套次>/prompts.json`（每个 IP 自己的，不共用）。

为什么提示词要单独存一份文件
============================
以前提示词写死在 `gen_video.py` 的 build_prompt() 里 —— 想微调一句话就得改代码，
改完还不敢确定有没有撞红线（S05 那次就是 DRY 才抓出来）。
现在提示词是**数据**：AI 可以生成、人可以微调、落盘前统一过红线、每次改动进 history。

★ 唯一硬规矩：**AI 生成的提示词必须先过 prompt_guard 才准落盘。**
  过不了就整批不落，被拒的内容写进 08_过程/prompts-rejected-<时间>.json 供复盘。

用法:
  prompts.py <IP> <套次> --show                  看当前提示词
  prompts.py <IP> <套次> --gen                   AI 按分段生成（整批落盘前过红线）
  prompts.py <IP> <套次> --optimize              AI 在现有基础上优化（旧版进 history）
  prompts.py <IP> <套次> --set 3 --text "..."    手动微调第 3 段
  prompts.py <IP> <套次> --gen --dry             只打印，不落盘
  prompts.py <IP> <套次> --to-en                 译成 prompts.en.json（出片才认这份）
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
sys.path.insert(0, BIN_DIR)

import vstudio as V                              # noqa: E402
from prompt_guard import check, GuardRules       # noqa: E402

CFG_PATH = os.path.join(ROOT, "config", "pipeline.json")

LAST_RAW = ""       # 最近一次 LLM 原文（PROMPTS_DEBUG=1 时打印，排查「段数为啥少了」用）

SYSTEM = """你是短视频出片的提示词工程师，服务一个竖屏口播 IP。

铁律（违反即作废）：
1. 只能用**正向描述**。禁止出现任何否定词：不要/不许/禁止/避免/去除/无需/没有/no/not/never/without/avoid/reduce。
   「不要太油」必须写成「皮肤呈哑光质感」这种正面说法。
2. 提示词是给视频生成模型看的，写**画面上能看见的东西**：表情、眉眼、手势幅度、镜头、光线，不要写抽象评价。
3. 每段 60-200 字，中文，一句话一个画面事实，不用排比和形容词堆砌。
4. prompt 必须**自包含**：把表情、动作、镜头都写进 prompt 里，不要只写在分字段里
   （分字段只是给人看的拆解，模型只读 prompt）。
5. 只输出 JSON，不要解释、不要 markdown 代码块。
输出格式：
{"segments":[{"i":1,"expression":"眉部放松，目光稳定","motion":"轻微点头，手势克制","camera":"固定机位，胸口以上中景","prompt":"完整提示词"}]}
"""


# ---------------- 文件 ----------------
def pp_path(ip_root, setname):
    return os.path.join(V.set_dirs(ip_root, setname)["05"], "prompts.json")


def load_prompts(ip_root, setname):
    p = pp_path(ip_root, setname)
    if not os.path.exists(p):
        return None, p
    with open(p, encoding="utf-8") as f:
        return json.load(f), p


def load_cfg():
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def rules_for(ip_root):
    cfg = load_cfg()
    ip_path = os.path.join(ip_root, "ip.json")
    ip = None
    if os.path.exists(ip_path):
        with open(ip_path, encoding="utf-8") as f:
            ip = json.load(f)
    return GuardRules(cfg, ip), (ip or {})


# ---------------- 分段 ----------------
def cues_from_srt(path):
    if not path or not os.path.exists(path):
        return []
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


def split_by_text(txt, target_chars=45):
    """没有 SRT 时按句读粗略切段。"""
    parts = re.split(r"(?<=[。！？!?；;])", txt)
    out, buf = [], ""
    for p in parts:
        buf += p
        if len(buf) >= target_chars:
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [(0.0, 0.0, s) for s in out] or [(0.0, 0.0, txt)]


def build_segments(ip_root, setname, target_seg_secs=8.0):
    """按字幕时间轴把文案切成段（每段约 target_seg_secs 秒）。"""
    dirs = V.set_dirs(ip_root, setname)
    srt = None
    for pat in (os.path.join(dirs["07"], "*.srt"), os.path.join(dirs["05"], "*.srt")):
        fs = sorted(glob.glob(pat))
        if fs:
            srt = fs[0]
            break
    cues = cues_from_srt(srt)
    if cues:
        segs, cur, t0 = [], [], cues[0][0]
        for c in cues:
            cur.append(c)
            if c[1] - t0 >= target_seg_secs:
                segs.append((t0, c[1], "".join(x[2] for x in cur)))
                cur, t0 = [], c[1]
        if cur:
            segs.append((t0, cur[-1][1], "".join(x[2] for x in cur)))
        return segs
    txtp = None
    for pat in ("*字幕文本*.txt", "*.txt", "*.md"):
        fs = [f for f in sorted(glob.glob(os.path.join(dirs["05"], pat)))
              if not os.path.basename(f).startswith("job")]
        if fs:
            txtp = fs[0]
            break
    if not txtp:
        return []
    txt = re.sub(r"\s+", "", open(txtp, encoding="utf-8-sig").read())
    return split_by_text(txt)


# ---------------- LLM ----------------
def load_llm_env():
    env = os.path.join(ROOT, "config", "llm.env")
    if os.path.exists(env):
        with open(env, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ip_must_rules(ip_root):
    """把 ip.json 的红线翻译成 LLM 听得懂的「必须写到」清单。

    ★ 不这么做的话，AI 十有八九会漏掉「眉部放松」这类 IP 特有要求 ——
      上一版实测 3 段全被红线拦下，原因都一样：缺眉。
      与其等它写完再拒，不如**先把要求写进 system**。
    """
    rules, _ = rules_for(ip_root)
    lines = []
    for key, spec in (rules.require_if or {}).items():
        if key.startswith("_") or not isinstance(spec, dict):
            continue
        if rules.ip_rules.get(key):
            lines.append(f"- {spec.get('hint','')}（关键词须出现其一："
                         f"{'/'.join(spec.get('words', []))}）")
    if rules.banned:
        lines.append(f"- 禁用词：{'/'.join(rules.banned)}")
    if rules.ip_rules.get("negative_free"):
        lines.append("- 再强调一遍：任何否定表达都不允许，只能正向描述")
    return "\n".join(lines)


def llm_json(user, system=None, timeout=180):
    load_llm_env()
    from llm_router import LLMRouter
    r = LLMRouter()
    d = r.chat([{"role": "system", "content": system or SYSTEM},
                {"role": "user", "content": user}],
               # ★ sensitive=True 是默认值，这里写出来是因为它是一条**安全边界**：
                #   喂进去的是客户台词，一旦有人图省事改成 False，这些台词就会流进
                #   「提示词可能被上游记录」的第三方免费池。显式写 = 改动时能被 review 到。
                sensitive=True)
    content = d["choices"][0]["message"].get("content", "")
    global LAST_RAW
    LAST_RAW = content
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError(f"LLM 没返回 JSON：{content[:300]}")
    return json.loads(m.group(0)), d.get("_provider", "?")


# ---------------- 落盘 ----------------
def seg_text(s):
    """一段提示词的完整文本 = 各分字段 + prompt。

    ★ 校验必须看拼接后的整体：LLM 常常把「眉部放松」写进 expression、
      prompt 里却漏掉。分字段也是要给人看的，**合起来才是模型看到的东西**。
    """
    return " ".join(str(x) for x in (s.get("expression", ""), s.get("motion", ""),
                                     s.get("camera", ""), s.get("prompt", "")) if x)


def reject_dump(ip_root, setname, data, issues):
    d = V.set_dirs(ip_root, setname)["08"]
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"prompts-rejected-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"issues": issues, "data": data}, f, ensure_ascii=False, indent=2)
    return p


def write_prompts(ip_root, setname, data, source):
    """★ 落盘前整批过红线，任何一段不过就整批不落。"""
    rules, _ = rules_for(ip_root)
    issues = []
    for s in data.get("segments", []):
        ok, iss = check(seg_text(s), rules)
        if not ok:
            issues.append({"i": s.get("i"), "issues": iss,
                           "text": s.get("prompt", "")[:120]})
    if issues:
        p = reject_dump(ip_root, setname, data, issues)
        return False, issues, p

    path = pp_path(ip_root, setname)
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    data.setdefault("_schema", "prompts/1.0")
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["source"] = source
    if old:
        data.setdefault("history", [])
        data["history"].append({"at": old.get("updated"),
                                "source": old.get("source"),
                                "segments": old.get("segments", [])})
        data["history"] = data["history"][-10:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True, [], path


# ---------------- 英文转写（提示词闭环） ----------------
# 为什么要多这一层英文转写：
#   出片提示词的**骨架是英文**（H3 的受理格式），而 05_文案/<套次>/prompts.json
#   工作台产出的是**中文分镜**。以前这两边互不相通 —— 人在界面里把提示词改得再好，
#   出片时一句都用不上（gen_video.py 根本不读它），界面还假装那是成片依据。
#
#   ⚠ 直接把中文塞进骨架是**不行**的（2026-10-27 S04 实测）：
#     不在 <d> 标记里的中文会被模型当成「画面上该出现的字」，全片烧出字幕。
#     所以必须在中间加一层「中文 → 英文」的转写，并且**转写结果必须不含一个汉字**。
#
#   这一层落成 prompts.en.json：gen_video.py 只在它存在时才采纳。
#   没有它就走老路（ip.json 的 prompt_vars），行为跟以前逐字节一致。
TR_SYSTEM = """你是给视频生成模型写画面提示词的英文译者。

任务：把中文分镜描述译成英文短句。

硬约束（违反即作废）：
1. 输出必须是纯英文，**一个中文字符都不能有**（汉字会被模型当成画面上要烧的字幕）。
2. 只能用**正向描述**，禁止 no/not/never/without/avoid 这类否定词。
   中文里的「不要皱眉」要译成正面说法，例如 His brows rest level and relaxed.
3. 只写**画面上能看见的事实**：眉、眼、嘴的状态，手势幅度，身体动作。
   不写抽象评价（「专业」「亲和」要落成具体的脸部和手部状态）。
4. 不写任何像字幕/标题/logo 的内容，不出现引号和换行。
5. 每句用英文句号结尾，单句不超过 30 个词。

输出 JSON（不要解释、不要 markdown 代码块）：
{"segments":[{"i":1,"expression_en":"His brows rest ...","perf_en":"His right hand ..."}]}
"""


def en_path(ip_root, setname):
    return os.path.join(V.set_dirs(ip_root, setname)["05"], "prompts.en.json")


def source_hash(data):
    """中文源文件指纹：人改过稿子就要重译，没改过就别浪费 LLM 调用。"""
    core = [{"i": s.get("i"), "expression": s.get("expression", ""),
             "motion": s.get("motion", ""), "camera": s.get("camera", "")}
            for s in (data.get("segments") or [])]
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def do_to_en(e, setname, force=False, as_json=False):
    """把 prompts.json 的中文分镜译成 prompts.en.json（出片才认这份）。"""
    data, p = load_prompts(e["path"], setname)
    if not data:
        sys.exit(f"✗ 没有提示词文件：{p}"
                 f"\n  先生成：vstudio prompts {e['name']} {setname} --gen")
    segs = data.get("segments") or []
    if not segs:
        sys.exit("✗ prompts.json 里没有 segments")

    out = en_path(e["path"], setname)
    h = source_hash(data)
    if os.path.exists(out) and not force:
        try:
            old = json.load(open(out, encoding="utf-8"))
        except Exception:
            old = {}
        if old.get("source_hash") == h:
            if as_json:
                print(json.dumps({"ok": True, "fresh": True, "path": out}))
            else:
                print(f"✓ prompts.en.json 已是最新（跟当前中文稿同源）：{out}")
                print("  改了中文稿请重跑本命令；要强制重译加 --force")
            return True

    payload = [{"i": s.get("i"),
                "expression": s.get("expression", ""),
                "motion": s.get("motion", ""),
                "camera": s.get("camera", "")} for s in segs]
    user = ("下面是一支竖屏口播视频各段的中文分镜（expression/motion/camera）。\n"
            "逐段译成英文：\n"
            "  expression_en = 这一段的眉/眼/嘴状态（照 expression 译，1 句）\n"
            "  perf_en = 这一段的手势与身体动作（照 motion 译，1 句；"
            "如果他只是自然端坐没有动作，就写 His hands rest still and low near his lap.）\n"
            "camera 不用译出，只用来理解语境。\n\n"
            + json.dumps(payload, ensure_ascii=False))

    try:
        d, prov = llm_json(user, system=TR_SYSTEM)
    except Exception as ex:
        sys.exit(f"✗ LLM 转写失败：{ex}"
                 f"\n  看路由状态：vstudio llm status")

    got = {int(x.get("i")): x for x in (d.get("segments") or [])
           if str(x.get("i", "")).lstrip("-").isdigit()}
    result, issues = [], []
    for s in segs:
        i = s.get("i")
        g = got.get(i) or {}
        row = {"i": i}
        for tag, target in (("expression_en", "expression_en"), ("perf_en", "perf_en")):
            v = str(g.get(target, "") or "").strip().strip('"').strip("'")
            cjk = re.findall(r"[\u4e00-\u9fff]", v)
            if cjk:
                issues.append(f"段{i} {tag} 里混进了中文：{''.join(cjk)[:30]}")
                continue
            if v and not v.endswith("."):
                v += "."
            row[tag] = v
        if not row.get("expression_en"):
            issues.append(f"段{i} 没译出 expression_en")
        if not row.get("perf_en"):
            issues.append(f"段{i} 没译出 perf_en")
        result.append(row)

    if issues:
        # ★ 绝不写半成品：留着旧的比写一份缺段的强 —— 缺段会导致那段悄悄退回默认值，
        #   出片照出，但人以为自己改的那版生效了，这是最难查的一类假片。
        print("✗ 这一批没有落盘：")
        for x in issues:
            print(f"   - {x}")
        print(f"\n  重跑一次即可（LLM 有随机性）：vstudio prompts {e['name']} {setname} --to-en --force")
        return False

    doc = {"_schema": "prompts-en/1.0",
           "_why": "出片提示词骨架是英文；中文表进不去（会被烧成字幕）。这份是prompts.json的英文转写，gen_video.py 只在它存在时采纳。",
           "ip": e["name"], "set": setname,
           "source_hash": h,
           "provider": prov,
           "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "segments": result}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    if as_json:
        print(json.dumps({"ok": True, "fresh": False, "path": out,
                          "segments": len(result), "provider": prov}))
    else:
        print(f"✓ 已写出 {out}（{len(result)} 段，路由 {prov}）")
        for r in result:
            print(f"  [{r['i']}] {r['expression_en']} {r['perf_en']}")
    return True


# ---------------- 命令 ----------------
def cmd_show(e, setname, as_json):
    data, path = load_prompts(e["path"], setname)
    if not data:
        print(f"没有提示词文件：{path}")
        print(f"  AI 生成：vstudio prompts {e['name']} {setname} --gen")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"{e['name']} · {setname}   来源 {data.get('source')}  "
          f"更新 {data.get('updated')}")
    gp = data.get("global") or {}
    if gp.get("prefix"):
        print(f"\n全局前缀：{gp['prefix']}")
    for s in data.get("segments", []):
        print(f"\n[{s.get('i')}] {s.get('t0', 0):.1f}-{s.get('t1', 0):.1f}s")
        print(f"    {s.get('prompt','')}")


def do_gen(e, setname, optimize=False, dry=False, as_json=False):
    ip_root = e["path"]
    segs = build_segments(ip_root, setname)
    if not segs:
        sys.exit("✗ 切不出段：05_文案/<套次> 里没有文案 txt，也没有 SRT")
    cfg = load_cfg()
    gp = cfg.get("global_prompt", {}).get("prefix", "")

    old, _ = load_prompts(ip_root, setname)
    if optimize and not old:
        sys.exit("✗ --optimize 需要先有一版提示词（先跑 --gen）")

    lines = [f"IP：{e['name']}",
             f"全局通用要求（每段都要遵守，不必重复写）：{gp}",
             f"共 {len(segs)} 段，请为每段各写一条提示词：", ""]
    for i, (t0, t1, txt) in enumerate(segs, 1):
        base = ""
        if optimize and old:
            b = next((s for s in old.get("segments", []) if s.get("i") == i), None)
            base = f"\n现有版本（在此基础上优化，保持正向描述）：{b.get('prompt')}" if b else ""
        lines.append(f"第 {i} 段 {t0:.1f}-{t1:.1f}s 台词：{txt}{base}")
    user = "\n".join(lines)

    sys_txt = SYSTEM + "\n本 IP 的硬性要求（漏一条即作废）：\n" \
        + (ip_must_rules(ip_root) or "- 无额外要求")
    # ★ 送上去的是整段客户台词。sensitive=True 只保证「不进免费池」，
    #   不代表「不出这台机器」。卡里 policy.allow_cloud_llm=false 的客户，这里必须停下。
    import ip_policy
    cloud_why = ip_policy.require_cloud(ip_root, e.get("name") or "这个 IP")
    print(f"内容上云：{cloud_why}", flush=True)
    print(f"向 LLM 请求 {len(segs)} 段提示词 …", flush=True)
    try:
        data, provider = llm_json(user, sys_txt)
    except Exception as ex:
        sys.exit(f"✗ LLM 调用失败：{ex}\n"
                 f"  看路由状态：vstudio llm status（或 python3 bin/llm_router.py status）")

    if os.environ.get("PROMPTS_DEBUG"):
        print("---- LLM 原文 ----\n" + LAST_RAW[:2000] + "\n------------------")
    got = {int(s["i"]): s for s in data.get("segments", []) if str(s.get("i")).isdigit()}
    if len(got) < len(segs):
        print(f"⚠ LLM 只返回 {len(got)}/{len(segs)} 段"
              f"（PROMPTS_DEBUG=1 看原文）—— 缺的段留空，请重跑或用 --set 补")
    segments = []
    for i, (t0, t1, txt) in enumerate(segs, 1):
        s = got.get(i, {})
        segments.append({"i": i, "t0": round(t0, 2), "t1": round(t1, 2),
                         "line": txt[:80],
                         "expression": s.get("expression", ""),
                         "motion": s.get("motion", ""),
                         "camera": s.get("camera", ""),
                         "prompt": s.get("prompt", "")})
    payload = {"ip": e["name"], "set": setname,
               "global": {"prefix": gp, "suffix": cfg.get("global_prompt", {}).get("suffix", "")},
               "segments": segments}

    print(f"供应商 {provider} —— 校验红线中 …")
    if dry:
        rules, _ = rules_for(ip_root)
        bad = []
        for s in segments:
            ok, iss = check(seg_text(s), rules)
            if not ok:
                bad.append({"i": s["i"], "issues": iss})
        if as_json:
            print(json.dumps({"dry": True, "segments": segments, "rejected": bad},
                             ensure_ascii=False, indent=2))
        else:
            for s in segments:
                mark = next(( "✗" for b in bad if b["i"] == s["i"]), "✓")
                print(f"{mark} [{s['i']}] {s['prompt']}")
            for b in bad:
                print(f"   [{b['i']}] 未过线：" + "；".join(b["issues"]))
        print("\n（--dry 不落盘）")
        return

    ok, issues, path = write_prompts(ip_root, setname, payload,
                                     "ai-optimize" if optimize else "ai-gen")
    if not ok:
        print("✗ 有段未过红线，整批不落盘：")
        for x in issues:
            print(f"   [{x['i']}] " + "；".join(x["issues"]))
        print(f"   被拒内容已存 {path}")
        sys.exit(1)
    print(f"✓ 已落盘 {path}（{len(segments)} 段）")


def do_set(e, setname, idx, text, as_json=False):
    ip_root = e["path"]
    data, path = load_prompts(ip_root, setname)
    if not data:
        sys.exit(f"✗ 还没有 prompts.json（先 --gen）：{path}")
    hit = next((s for s in data["segments"] if s["i"] == idx), None)
    if not hit:
        sys.exit(f"✗ 没有第 {idx} 段（现有 {[s['i'] for s in data['segments']]}）")
    rules, _ = rules_for(ip_root)
    ok, issues = check(text, rules)
    if not ok:
        print("✗ 未过红线，不落盘：")
        for s in issues:
            print("   -", s)
        sys.exit(1)
    hit["prompt"] = text
    hit["manual"] = True
    # write_prompts 还会对「分字段 + prompt 拼起来的整体」再过一次线。
    # 以前这一句的返回值被忽略 —— 整体没过线时其实没落盘，却照样打印「✓ 已更新」，
    # 人改完看到成功、回头发现文件还是旧的。
    ok2, iss2, p2 = write_prompts(ip_root, setname, data, "manual")
    if not ok2:
        print("✗ 整段拼起来仍未过红线，没落盘：")
        for x in iss2:
            print(f"   [{x['i']}] " + "；".join(x["issues"]))
        print(f"   被拒内容已存 {p2}")
        sys.exit(1)
    print(f"✓ 第 {idx} 段已更新 → {p2}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="提示词工作台")
    ap.add_argument("ip", nargs="?", default="洁斌")
    ap.add_argument("set")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--gen", action="store_true")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--set", dest="seg", type=int,
                    help="手动改第 N 段（★ 与位置参数「套次」重名，故 dest 用 seg）")
    ap.add_argument("--text", help="与 --set 同用的提示词文本")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--to-en", dest="to_en", action="store_true",
                    help="把中文分镜译成 prompts.en.json（★出片只认这份英文）")
    ap.add_argument("--force", action="store_true",
                    help="与 --to-en 同用：不管中文稿有没有变都重译一次")
    a = ap.parse_args(argv)

    e = V.resolve_ip(a.ip)
    sets = V.list_sets(e["path"])
    if a.set not in sets:
        hit = [s for s in sets if s.startswith(a.set)]
        if not hit:
            sys.exit(f"✗ 没有这套：{a.set}（现有 {' / '.join(sets) or '无'}）")
        a.set = hit[0]

    if a.text:
        if not a.seg:
            sys.exit("--text 需要和 --set N 一起用")
        do_set(e, a.set, a.seg, a.text, a.json)
    elif a.to_en:
        ok = do_to_en(e, a.set, force=a.force, as_json=a.json)
        if not ok:
            sys.exit(1)
    elif a.gen or a.optimize:
        do_gen(e, a.set, optimize=a.optimize, dry=a.dry, as_json=a.json)
    else:
        cmd_show(e, a.set, a.json)


if __name__ == "__main__":
    main()
