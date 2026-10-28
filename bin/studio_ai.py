#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作室里「要动脑子」的几步：AI 选题、AI 起草档案卡。

为什么单独一个文件
==================
`ui_server.py` 只该干「转发 CLI + 端茶倒水」的活。凡是需要调大模型的，
单独放这里，好处有三个：

  1. 能直接在终端跑（`python3 bin/studio_ai.py topics 洁斌`），不必开网页；
  2. 换 UI / 换 MCP / 接 n8n 都复用这一份，不会三套提示词各写一遍然后打架；
  3. 涉密边界只有一处需要审 —— 见下面 SENSITIVE 那段。

★ 这类内容一律 sensitive=True
=============================
人设圣经、档案卡、需求稿，全是一个真人的形象设定和客户商业判断。
送进「提示词可能被上游记录」的第三方免费池，是拿客户的商业机密换几厘钱。
所以本模块所有调用都是 sensitive=True（只走付费池），且**不提供关掉它的参数** ——
需要免费池的场合（视觉验收那种截屏抽查）不在这个文件里，去 llm_router.vision。
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# 人设材料最多喂多少字。整本圣经 17KB 全塞进去会挤掉真正需要的指令，
# 而且越靠后的内容模型越容易忽略 —— 宁可按优先级截断。
PERSONA_BUDGET = 6000


# ---------------------------------------------------------------- 基础
def load_env():
    p = os.path.join(ROOT, "config", "llm.env")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def chat_json(system, user, max_tokens=2000, prefer=None, temperature=0.7):
    """要一段 JSON 回来。模型爱把 JSON 包在 ```json 围栏里或前面加解释，都要剥掉。"""
    load_env()
    from llm_router import LLMRouter
    r = LLMRouter()
    d = r.chat([{"role": "system", "content": system},
                {"role": "user", "content": user}],
               max_tokens=max_tokens, temperature=temperature,
               sensitive=True,        # ← 见文件头。这行不许改。
               prefer=prefer or None)
    content = (d["choices"][0]["message"].get("content") or "").strip()
    txt = content
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
    if m:
        txt = m.group(1)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise ValueError(f"模型没给 JSON：{content[:300]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as ex:
        raise ValueError(f"JSON 解析失败（{ex}）：{m.group(0)[:300]}")
    return data, d.get("_provider", "?")


def _read(path, limit=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            s = f.read()
    except Exception:
        return ""
    return s[:limit] if limit else s


def _short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 人设材料
def persona_material(ip_root):
    """把 01_人设与规范 里的 md 拼成一段材料。

    排序有讲究：文件名带「人设」「圣经」「IP」的排最前（那是主干），
    其余按大小降序 —— 大的通常信息多。总预算 PERSONA_BUDGET 字。
    """
    d = os.path.join(ip_root, "01_人设与规范")
    files = [f for f in glob.glob(os.path.join(d, "*.md"))
             if not os.path.basename(f).startswith(".")]
    if not files:
        return "", []

    def rank(f):
        n = os.path.basename(f)
        key = 0 if any(k in n for k in ("人设", "圣经", "IP", "ip")) else 1
        try:
            sz = -os.path.getsize(f)
        except Exception:
            sz = 0
        return (key, sz)

    files.sort(key=rank)
    used, parts, names = 0, [], []
    for f in files:
        room = PERSONA_BUDGET - used
        if room < 300:
            break
        body = _read(f, room)
        if not body.strip():
            continue
        used += len(body)
        names.append(os.path.basename(f))
        parts.append(f"### 文件：{os.path.basename(f)}\n{body}")
    return "\n\n".join(parts), names


def ip_context(ip):
    """人设材料 + 档案卡 + 已有套次。缺什么就少给什么，不编。"""
    from vstudio import resolve_ip, load_card, list_sets, load_job, build_job
    e = resolve_ip(ip)
    root = e["path"]
    persona, pfiles = persona_material(root)
    card = load_card(root) or {}
    try:
        sets = list_sets(root)
        titles = []
        for s in sets:
            j = load_job(root, s) or build_job(e, s)
            titles.append(f"{s}：{j.get('title') or '（无标题）'}")
    except Exception:
        titles = []
    return {
        "name": e["name"], "root": root,
        "persona": persona, "persona_files": pfiles,
        "card": card, "titles": titles,
    }


# ---------------------------------------------------------------- 选题
TOPIC_SYSTEM = """你是一个短视频选题策划，专门给「AI 数字人口播」账号出题。
你面前有这个 IP 的人设圣经、已有选题清单、以及客户的硬性红线。

出题要求：
1. 只在这个人设能自然开口的范围内出题。人设里没写过的身份、经历、专业领域，绝不编。
2. 每条必须是一个「具体的主张或反常识判断」，不是「聊聊XX」这种空题。
   判断题比话题好使：观众要么认同要么反对，评论区才动。
3. 回避已有清单里出现过的角度。同一个意思换个说法也算重复。
4. 红线里禁止的表达，标题和钩子里都不能出现。
5. 口播前 3 秒靠钩子留人，钩子要能自己站住，不能靠上下文。

返回 JSON，不要任何解释文字：
{"topics":[{"title":"8字以内的短标题","hook":"开场第一句话，25字以内，要抓人","angle":"这条的核心判断是什么，30字以内","why":"为什么这个号适合讲这条，25字以内"}],
 "note":"你观察到的人设特征或提醒，40字以内"}"""


def suggest_topics(ip, n=5, hint="", prefer=None):
    ctx = ip_context(ip)
    if not ctx["persona"].strip():
        raise RuntimeError(
            f"{ctx['name']} 的 01_人设与规范/ 里没有 md 材料。\n"
            "AI 选题必须先读人设 —— 没有材料就只能瞎编，那是害人。\n"
            "先往那个目录放一份人设/背景说明，或用「对话式建档」让它顺手写出来。")
    # 人设 + 需求 + 红线一起送出去，等于把客户的商业底牌交出去；
    # 先问一句这张卡允不允许（默认不许）。
    import ip_policy
    cloud_why = ip_policy.require_cloud(ctx["root"], ctx["name"])

    card = ctx["card"]
    redlines = _texts(card.get("redlines"))
    needs = card.get("needs") or {}
    user = f"""# 这个 IP
名字：{ctx['name']}
类型：{card.get('type', '?')}    一句话定位：{card.get('role', '')}

# 客户给的需求
{json.dumps(needs, ensure_ascii=False, indent=1)}

# 硬性红线（绝对不能碰）
{chr(10).join('- ' + str(r) for r in redlines) if redlines else '（档案卡里没记红线）'}

# 人设材料
{ctx['persona']}

# 已经做过的选题（不要重复这些角度）
{chr(10).join('- ' + t for t in ctx['titles']) if ctx['titles'] else '（还没有套次）'}

# 这次的要求
出 {n} 条新选题。{('额外要求：' + hint) if hint.strip() else ''}"""

    data, provider = chat_json(TOPIC_SYSTEM, user, max_tokens=1800,
                               prefer=prefer, temperature=0.85)
    topics = data.get("topics") or []

    # 红线过一遍：模型不知道「禁」字怎么写，得替它查
    banned = []
    try:
        from prompt_guard import GuardRules
        rules = GuardRules.from_files(
            os.path.join(ROOT, "config", "pipeline.json"),
            os.path.join(ctx["root"], "ip.json"))
        banned = list(rules.banned)
    except Exception:
        pass

    kept, dropped = [], []
    for t in topics:
        blob = " ".join(str(t.get(k, "")) for k in ("title", "hook", "angle"))
        hit = [w for w in banned if w and w in blob]
        if hit:
            dropped.append({"title": t.get("title", ""), "reason": "禁用词 " + "/".join(hit)})
            continue
        kept.append({
            "title": _short(t.get("title"), 20),
            "hook": _short(t.get("hook"), 60),
            "angle": _short(t.get("angle"), 60),
            "why": _short(t.get("why"), 60),
        })

    return {
        "ip": ctx["name"], "provider": provider,
        "topics": kept, "dropped": dropped,
        "note": _short(data.get("note"), 80),
        "persona_files": ctx["persona_files"],
        "persona_chars": len(ctx["persona"]),
        "existing": ctx["titles"],
        # 把「凭什么允许上云」一起带出去：临时放行也留痕
        "cloud_policy": cloud_why,
    }


def _texts(items):
    """把档案卡里那种 [{'at':..., 'text':...}] 的条目抽成纯文本。

    ★ 别直接 str()：会喂给模型一堆 `{'at': '2026-10-28 21:35', 'text': ...}`，
      既浪费 token 又让"禁止"这类关键约束淹没在噪音里。
    """
    out = []
    for x in (items or []):
        if isinstance(x, dict):
            t = x.get("text") or x.get("note") or ""
            if str(t).strip():
                out.append(str(t).strip())
        elif str(x).strip():
            out.append(str(x).strip())
    return out


# ---------------------------------------------------------------- 爆款文案
SCRIPT_STANDARD = os.path.join(ROOT, "templates", "script_standard.md")


def load_standard():
    """爆款标准是**判据**，必须存在。

    读不到就停 —— 凭模型自己的口味写出来的稿子不算数，
    而且下次换个人来跑，结果会完全不一样。
    """
    if not os.path.exists(SCRIPT_STANDARD):
        raise RuntimeError(
            "缺少爆款标准：" + SCRIPT_STANDARD + "\n"
            "这是全库共用的判据（从主号 S01–S05 已跑量的稿子提纯出来的）。\n"
            "没有它，模型只能凭自己的网感自由发挥 —— 出稿质量会飘，而且不可复现。")
    with open(SCRIPT_STANDARD, encoding="utf-8") as f:
        return f.read()


def _brief_text(root, setname):
    """这套片子的需求稿（人手写的那份）。没有就空着 —— 不编。"""
    base = os.path.join(root, "05_文案")
    if not os.path.isdir(base) or not setname:
        return "", None
    cand = [x for x in sorted(os.listdir(base)) if x.startswith(setname)]
    if not cand:
        return "", None
    p = os.path.join(base, cand[0], "brief.md")
    if not os.path.exists(p):
        return "", None
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()[:4000], p
    except Exception:
        return "", None


def _past_scripts(root, limit=3):
    """这个号最近写好的几条文案。

    给模型看两件事：① 这个号真实的口吻和长度是多少；
    ② 别连着几条用同一个开头套路 —— 同一个号连发，观众一眼看出是模板。
    """
    base = os.path.join(root, "05_文案")
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if "字幕文本" in fn and fn.endswith(".txt"):
                try:
                    with open(os.path.join(p, fn), encoding="utf-8") as f:
                        t = f.read().strip()
                    if t:
                        out.append({"set": d, "text": t[:500]})
                except Exception:
                    pass
        if len(out) >= limit:
            break
    return out[:limit]


SCRIPT_SYSTEM = """你是「AI 数字人口播」账号的文案，写的是**要念出来的口播稿**，不是文章。

你会拿到：这个 IP 的人设、客户的具体需求、这个账号的硬性红线、
一份《爆款口播文案标准》（同一个工作室主号跑量的稿子提纯出来的判据）、
以及这个号最近发过的几条稿（用来对齐口吻、避开重复套路）。

写作要求，按重要性排序：
1. **严格照标准文件里的五段骨架写**：钩子 / 共情 / 反转或算账 / 降门槛 / 落点。
   落点必须用标准里给的那句固定话术，一个字都别改 —— 它是资产，不是文案。
2. 只写这个人设能自然开口的话。人设里没写过的身份、经历、专业领域，绝不编。
3. 字数按标准（默认 130–170 字）。宁短不长。
4. 全程「你」，短句，能一口气念下来。写完自己念一遍，卡舌头就重写。
5. 红线里的表达一个都不许出现。
6. 学标准里那几条示例的**结构**，不要抄它们的**句子**。
7. 不要另起"关注我""点赞收藏"这类尾巴 —— 落点就是结尾。

返回 JSON，不要任何解释文字：
{"hook_type":"反常识/证言/威胁/过程/算账（选一个）",
 "landing":"结尾那句固定话术（若已给定就原样用；没给定就按这个客户的业务拟一条）",
 "landing_new":true或false（"是不是这次新拟的"）,
 "lines":["屏幕上逐行出现的字幕，每行 2-12 字，按口语换气切"],
 "script":"把 lines 连起来的完整口播稿，一句一行，最后一行必须是 landing",
 "claims":["这条里承诺了什么，逐条列出（必须能被兑现）"],
 "self_check":{"hook_alone":true,"numbers_used":3,"no_self_praise":true,"landing_fixed":true},
 "note":"你拿不准的地方，40 字以内"}

★ 关于落点：如果输入里给了「本号固定落点」，**必须原样放在最后一行，一个字都不许改**；
  只有明确写着「还没定落点」时，才可以自己拟 —— 并在 landing_new 里标 true，
  让人知道这句是新的、需要他确认。落点缺失是最常见的翻车点：稿子讲得好好的，最后没有让人动手。"""


def write_script(ip, setname="", topic="", tone="", prefer=None, length="standard"):
    """按角色需求 + 主号爆款标准，写一条完整口播稿。

    ★ 只出稿，不落盘。落盘由调用方决定（`--write`），
      因为覆盖别人写好的文案是不可逆的，得让动手的人自己确认。
    """
    ctx = ip_context(ip)
    standard = load_standard()

    card = ctx["card"]
    needs = card.get("needs") or {}
    habits = _texts(card.get("habits"))
    redlines = _texts(card.get("redlines"))

    import ip_policy
    cloud_why = ip_policy.require_cloud(ctx["root"], ctx["name"])

    # 本号的固定落点。没有就让模型拟一条并标出来 —— 不能每次临时编，
    # 每次不一样等于没有落点，观众也记不住要他干什么。
    landing = str(card.get("landing") or "").strip()

    brief, brief_path = _brief_text(ctx["root"], setname) if setname else ("", None)
    past = _past_scripts(ctx["root"])

    words = "标准版 130–170 字" if length == "standard" else "短版 80–100 字"

    user = f"""# 这是谁
名字：{ctx['name']}
类型：{card.get('type', '?')}    一句话定位：{card.get('role', '')}

# 客户的需求（他本人怎么说）
{json.dumps(needs, ensure_ascii=False, indent=1) if needs else '（档案卡里没记需求 —— 只能靠人设推，写保守些）'}

# 这套片子的需求稿（最贴身的约束，优先于上面）
{brief.strip() if brief.strip() else '（这套还没写 brief.md。请只依据人设和客户需求，不要自己发明具体承诺）'}

# 硬性红线（一条都不能碰）
{chr(10).join('- ' + r for r in redlines) if redlines else '（档案卡里没记红线）'}

# 他的说话习惯
{chr(10).join('- ' + h for h in habits) if habits else '（没记）'}

# 本号固定落点
{landing if landing else '（还没定落点 —— 这次请你按这个客户的业务拟一条，并在 landing_new 标 true）'}

# 人设材料
{ctx['persona']}

# 这个号最近发过的稿（对齐口吻，但不要重复这些开头和句式）
{chr(10).join(f'【{p["set"]}】{p["text"]}' + chr(10) for p in past) if past else '（还没有发过的）'}

# 《爆款口播文案标准》
{standard}

# 这次要写的
{('题目：' + topic) if topic.strip() else '题目：自己从这个号能讲的范围里挑一个最该现在发的（不要跟上面发过的重复角度）'}
{(chr(10) + '额外要求：' + tone) if tone.strip() else ''}
字数：{words}"""

    def one_pass(extra=""):
        d, pv = chat_json(SCRIPT_SYSTEM, user + extra, max_tokens=2400,
                          prefer=prefer, temperature=0.8)
        ln = [str(x).strip() for x in (d.get("lines") or []) if str(x).strip()]
        sc = str(d.get("script") or "").strip()
        if not sc and ln:
            sc = "\n".join(ln)
        if not sc:
            raise RuntimeError("模型没给出文案正文（返回里 script 和 lines 都是空的）")
        return d, pv, ln, sc

    data, provider, lines, script = one_pass()

    # ★ 字数不够就自动重来一次。
    #   实测模型很容易只写「钩子 + 落点」两段（93 字 / 18 秒），五段骨架塌成两段 ——
    #   这种稿子交给人，人要么自己补、要么直接发出去变短命片。
    #   提示词里写了字数它也不一定听，所以要拿结果去卡、卡不住就带着具体差距再问一遍。
    lo, hi = (120, 190) if length == "standard" else (70, 110)
    c0 = len(re.sub(r"\s", "", script))
    if c0 < lo * 0.85:
        try:
            data, provider, lines, script = one_pass(
                f"\n\n【上一版只有 {c0} 字，太短了】"
                f"要求 {lo}–{hi} 字。五段骨架（钩子/共情/反转或算账/降门槛/落点）"
                f"每段都要落地，不要压缩成两句。重写一版。")
        except Exception:
            pass        # 重写失败就用第一版，下面自检会如实报出来

    # 红线替模型查一遍：它不知道「禁」字怎么写
    banned = []
    try:
        from prompt_guard import GuardRules
        rules = GuardRules.from_files(
            os.path.join(ROOT, "config", "pipeline.json"),
            os.path.join(ctx["root"], "ip.json"))
        banned = list(rules.banned)
    except Exception:
        pass
    hit = sorted({w for w in banned if w and w in script})
    redline_hit = [r for r in redlines if r and r in script]

    # 落点：已经有固定话术的，必须原样出现在最后；新拟的要标出来等人确认
    new_landing = str(data.get("landing") or "").strip()
    final_landing = landing or new_landing
    landing_ok = bool(final_landing) and final_landing.replace(" ", "") in script.replace(" ", "")

    # ★ 模型十次有八次只在 landing 字段里给一句，正文里就是不写（实测两次都是）。
    #   落点是确定的一句话，不该指望模型自觉 —— 自己拼上去。
    #   漏了落点等于整条片子白讲：观众听完了，不知道要他干什么。
    if final_landing and not landing_ok:
        script = script.rstrip() + "\n" + final_landing
        if not lines or final_landing not in lines[-1]:
            lines.append(final_landing)
        landing_ok = True

    chars = len(re.sub(r"\s", "", script))
    checks = {
        "chars": chars,
        "est_secs": round(chars / 5.0, 1),      # 实测语速 5 字/秒
        "hook_alone": bool(lines and len(lines[0]) <= 20),
        "numbers_used": len(re.findall(r"\d+|一张|两条|十秒|二十分钟|三天|一碗|一份", script)),
        "no_banned_word": not hit,
        "no_redline": not redline_hit,
        "landing_ok": landing_ok,
        "landing_is_new": not landing and bool(new_landing),
    }
    problems = []
    if hit:
        problems.append("命中禁用词：" + "/".join(hit))
    if redline_hit:
        problems.append("踩到红线：" + "/".join(redline_hit[:3]))
    if not landing_ok:
        problems.append("结尾没有落点 —— 观众不知道要他干什么（这是最常见的一种白写）")
    if chars > hi:
        problems.append(f"太长（{chars} 字，标准 {lo}–{hi}）—— 超了就得删，删哪儿都疼")
    if chars < lo:
        problems.append(f"太短（{chars} 字，标准 {lo}–{hi} ≈ {round(lo / 5)}–{round(hi / 5)} 秒）"
                        f" —— 五段骨架撑不起来，快发给人的片子会显得没内容")
    checks["problems"] = problems
    checks["pass"] = not problems
    checks["target_chars"] = [lo, hi]

    return {
        "ip": ctx["name"], "set": setname, "provider": provider,
        "topic": topic or "(模型自选)", "hook_type": str(data.get("hook_type") or ""),
        "script": script, "lines": lines,
        "landing": final_landing, "landing_is_new": checks["landing_is_new"],
        "claims": [str(x) for x in (data.get("claims") or [])][:6],
        "note": _short(data.get("note"), 80),
        "checks": checks,
        "brief_path": brief_path,
        "past_count": len(past),
        "standard": SCRIPT_STANDARD,
        "cloud_policy": cloud_why,
    }


def save_script(result, ip, setname):
    """把出的稿子写进这套片子的文案文件。

    ★ 目标非空就先进废品站式的备份（.bak.<时间>），绝不静默覆盖 ——
      这是别人可能已经改过的稿子。
    """
    from vstudio import resolve_ip
    import datetime as _dt
    e = resolve_ip(ip)
    base = os.path.join(e["path"], "05_文案")
    cand = [x for x in sorted(os.listdir(base)) if x.startswith(setname)] if os.path.isdir(base) else []
    folder = os.path.join(base, cand[0]) if cand else os.path.join(base, setname)
    os.makedirs(folder, exist_ok=True)
    name = (cand[0] if cand else setname) + "_字幕文本.txt"
    path = os.path.join(folder, name)
    bak = None
    if os.path.exists(path) and open(path, encoding="utf-8").read().strip():
        bak = path + ".bak." + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        import shutil as _sh
        _sh.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.write(result["script"].rstrip() + "\n")
    return {"path": path, "backup": bak}


# ---------------------------------------------------------------- 建档草案
CARD_SYSTEM = """你是短视频工作室的客户建档助理。客户会用自己的话介绍自己和需求（可能很口语、很零碎）。

你的活是把它整理成一份能直接用的档案卡草案，并且**区分事实和推测**：
- 客户明说过的 → 直接写进对应字段
- 客户没说但你不得不填的（比如标签）→ 选最保守的那个
- 拿不准的 → 留空字符串，不要编

字段说明：
- name：目录名，只能是中文/字母/数字/下划线，不能用空格和符号。用品牌名或人名，别用「客户A」这种。
- aliases：口头叫法，数组。真人名、简称、昵称都要收进来（口头叫人名，目录是品牌名，没别名会重复建档）。
- type：只能是「客户」或「本人IP」。给别人做号就是「客户」。
- tag：套次前缀，1-2 个大写字母。
- role：一句话定位，20 字以内，说清「谁 + 讲什么 + 给谁看」。
- needs：对象，键用中文。常见键：内容方向、出片频率、交付平台、调性、备注。
  交付平台是数组。"出片频率"要写成"每周3条"这种可执行的。
- redlines：数组。客户明说不能碰的。没明说就留空数组 —— **不要替客户发明红线**。
- habits：数组。客户在沟通里流露的偏好（"不喜欢太快的语速"这种）。
- persona_md：对象，键是文件名（如 "人设圣经.md"），值是 markdown 正文。
  写 2 篇即可：一篇人设（身份/经历/口头禅/说话方式/受众/禁区），
  一篇内容方向（选题范围/结构习惯/开头钩子风格）。每篇 400-800 字，
  用客户自己的语气，不要写成广告文案。

返回 JSON，不要解释：
{"name":"","aliases":[],"type":"客户","tag":"","role":"","space":"",
 "needs":{},"redlines":[],"habits":[],
 "persona_md":{"文件名.md":"正文"},
 "questions":["还需要问客户的关键问题，最多3条"],
 "summary":"你对这个客户的理解，一句话，30字以内"}"""


def draft_card(text, prefer=None, ip_hint=""):
    if not (text or "").strip():
        raise RuntimeError("先说说这个客户是谁、要做什么")
    user = f"""客户原话：

{text.strip()}

{f'补充：{ip_hint}' if ip_hint.strip() else ''}"""
    # 已有的 IP 就先问卡允不允许；新客户还没有卡，用户是主动把这段描述交给 AI 整理的，
    # 默认放行，但结果里会带一句「这段描述走过云端」，让人知道发生了什么。
    cloud_why = "新建档案，还没有 card.json，按用户主动提交处理"
    if ip_hint.strip():
        try:
            from vstudio import resolve_ip
            e = resolve_ip(ip_hint)
        except SystemExit:
            e = None      # ip_hint 只是「客户姓林」这类补充，不是已有 IP，不算绕规矩
        except Exception:
            e = None
        if e:
            import ip_policy
            cloud_why = ip_policy.require_cloud(e["path"], e["name"])
    data, provider = chat_json(CARD_SYSTEM, user, max_tokens=3200,
                               prefer=prefer, temperature=0.5)

    # 目录名必须能当文件夹用，模型给的常常带空格、括号、斜杠 —— 就地清掉
    name = re.sub(r"[^\w\u4e00-\u9fff]", "", str(data.get("name") or ""))
    if not name:
        raise RuntimeError("模型没给出可用的目录名，换个说法再试")

    need = data.get("needs") or {}
    if not isinstance(need, dict):
        need = {"备注": str(need)}
    need = {str(k): v for k, v in need.items()}

    def strs(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if not v:
            return []
        return [str(v).strip()]

    md = data.get("persona_md") or {}
    if not isinstance(md, dict):
        md = {}
    md = {re.sub(r"[^\w\u4e00-\u9fff.]", "", str(k)) or "人设圣经.md": str(v)
          for k, v in md.items() if str(v).strip()}

    return {
        "provider": provider,
        "draft": {
            "name": name,
            "aliases": strs(data.get("aliases")),
            "type": "客户" if str(data.get("type")) != "本人IP" else "本人IP",
            "tag": re.sub(r"[^A-Z]", "", str(data.get("tag") or "").upper())[:2],
            "role": _short(data.get("role"), 40),
            "space": re.sub(r"[^\w\u4e00-\u9fff]", "", str(data.get("space") or "")) or (name + "资产"),
            "needs": need,
            "redlines": strs(data.get("redlines")),
            "habits": strs(data.get("habits")),
        },
        "persona_md": md,
        "questions": [str(q) for q in (data.get("questions") or [])][:3],
        "summary": _short(data.get("summary"), 60),
        "cloud_policy": cloud_why,
    }


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="工作室 AI：选题 / 建档草案")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("topics", help="按人设出选题")
    t.add_argument("ip")
    t.add_argument("-n", type=int, default=5)
    t.add_argument("--hint", default="")
    t.add_argument("--prefer", default=None, help="首选车道名")
    t.add_argument("--json", action="store_true", help="结果写成 JSON（给界面读）")
    t.add_argument("--out", default="", help="JSON 落到这个文件")

    c = sub.add_parser("card-draft", help="自然语言 → 档案卡草案")
    c.add_argument("text")
    c.add_argument("--prefer", default=None)
    c.add_argument("--json", action="store_true")
    c.add_argument("--out", default="")

    s = sub.add_parser("script", help="按角色需求 + 主号爆款标准，写一条口播稿")
    s.add_argument("ip")
    s.add_argument("--set", default="", help="这套片子的编号（会去读它的 brief.md）")
    s.add_argument("--topic", default="", help="题目；不给就让 AI 自选")
    s.add_argument("--tone", default="", help="额外要求")
    s.add_argument("--length", default="standard", choices=["standard", "short"])
    s.add_argument("--prefer", default=None, help="首选车道")
    s.add_argument("--write", action="store_true",
                   help="写进 05_文案/<套次>/（原稿非空会先备份）")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out", default="")

    p = sub.add_parser("persona", help="看某 IP 的人设材料有多少字进了 context")
    p.add_argument("ip")

    a = ap.parse_args()

    def emit(payload):
        """--out 给机器，stdout 给人。两者分开，界面的日志才不会被 JSON 糊住。"""
        if a.out:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)

    try:
        if a.cmd == "topics":
            r = suggest_topics(a.ip, a.n, a.hint, a.prefer)
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(f"通道 {r['provider']}   人设 {r['persona_chars']} 字 "
                  f"（{', '.join(r['persona_files']) or '无'}）")
            for i, x in enumerate(r["topics"], 1):
                print(f"\n{i}. {x['title']}\n   钩子：{x['hook']}\n   判断：{x['angle']}")
            if r["dropped"]:
                print(f"\n被红线拦下 {len(r['dropped'])} 条：" +
                      "；".join(f"{d['title']}（{d['reason']}）" for d in r["dropped"]))
            if r["note"]:
                print(f"\n模型备注：{r['note']}")
        elif a.cmd == "card-draft":
            r = draft_card(a.text, a.prefer)
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif a.cmd == "script":
            r = write_script(a.ip, a.set, a.topic, a.tone, a.prefer, a.length)
            if a.write:
                if not a.set:
                    raise RuntimeError("--write 要配 --set：稿子得知道放进哪一套")
                saved = save_script(r, a.ip, a.set)
                r["saved"] = saved
                if not a.json:
                    print(f"已写入 {saved['path']}")
                    if saved["backup"]:
                        print(f"原稿备份 → {saved['backup']}")
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            c = r["checks"]
            print(f"通道 {r['provider']}   钩子类型 {r['hook_type'] or '?'}   "
                  f"{c['chars']} 字 ≈ {c['est_secs']} 秒")
            print("-" * 56)
            for ln in (r["lines"] or r["script"].splitlines()):
                print(ln)
            print("-" * 56)
            if r["landing_is_new"]:
                print(f"落点（这次新拟的，确认后存进 card.json 的 landing）：「{r['landing']}」")
            elif r["landing"]:
                print(f"落点（本号固定话术）：「{r['landing']}」")
            if r["claims"]:
                print("这条承诺了：" + "；".join(r["claims"]))
            if c["problems"]:
                print("⚠ 自检没过：" + "；".join(c["problems"]))
                print("  （稿子照样给你，改完再落盘。别把超长的稿子直接拿去配音）")
            else:
                print("✓ 自检过了（字数/红线/钩子都在标准内）")
            if r["note"]:
                print(f"模型备注：{r['note']}")
        elif a.cmd == "persona":
            ctx = ip_context(a.ip)
            print(f"{ctx['name']}：{len(ctx['persona'])} 字，"
                  f"来自 {ctx['persona_files'] or '（空）'}")
            print(f"已有套次 {len(ctx['titles'])} 个")
    except Exception as ex:
        print(f"✗ {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
