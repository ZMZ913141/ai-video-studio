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
import array
import glob
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import time

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


# ★ 文案去重的**唯一真源**在 vstudio.py（script_norm / dedupe_script）。
#   放那边是因为落盘前那道闸（vstudio / ui_server）也要用同一份逻辑 ——
#   两处各写一遍，迟早不一致 = 迟早有一处漏。
from vstudio import dedupe_script, script_norm as _norm   # noqa: E402


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
    # ★ 返工记录（自动学习闭环的「用」那一半）
    #   以前踩过的坑只活在人脑里和 docs/ 里，模型每次都当没发生过，
    #   同一个「后段过曝」能返工三次。现在出稿时把历史教训一起给模型。
    try:
        from learning import digest as _digest
        lessons = _digest(ip)
    except Exception:
        lessons = ""          # 学习库读不了不该拖垮出稿
    return {
        "name": e["name"], "root": root,
        "persona": persona, "persona_files": pfiles,
        "card": card, "titles": titles, "lessons": lessons,
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

    # 本号的落点。★ 分两层（2026-10-29 实测踩到）：
    #   套次自己的 landing.md > 档案卡里的 landing。
    #   以前只有卡里一条，落点里的货品词（「这盘雪花」「你先吃」）是**某一套的**，
    #   写到别的套次上就前后对不上（讲汤的片子结尾说「这盘雪花」）。
    #   逻辑在 vstudio.resolve_landing（唯一真源）。
    from vstudio import resolve_landing
    landing, landing_src = resolve_landing(ctx["root"], setname)

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

# 这个 IP 踩过的坑（真实发生过的返工，不是猜测）
{ctx.get('lessons') or '（还没有返工记录）'}

# 本号落点
{landing if landing else '（还没定落点 —— 这次请你按这个客户的业务拟一条，并在 landing_new 标 true）'}

★ 落点的规矩（很重要，写错整条片子就废了）：
  - 落点的**句式骨架**照抄（比如「到店说一句「刷到你们家视频」，这__我请你尝。你先__，合口再来。」），
    它是这个号的固定动作，观众靠它记住你。
  - 但落点里的**货品词必须跟本条片子讲的东西对上**：
    讲汤就写「这锅汤／你先喝」，讲肉就写「这盘雪花／你先吃」。
    照抄别的片子的货品词 → 观众听到「这盘雪花」但全片在讲汤 → 直接划走。
  - 落点**只写一遍**，放在最后一句。不要拆成两行后又把整块重复一次。
  {'' if landing else ''}

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
        # ★ 模型有时把 script 字段填成 **list 的 repr**（"['第一句', '第二句']"），
        #   那不是正文，是 Python 打印出来的数据结构。
        #   实测（2026-10-29 老王 M01）：模型这么返过一次 → 稿子原样落盘，
        #   文件里全是引号和方括号，TTS 会照着念这些符号。
        #   这里认出来就改用 lines 拼正文 —— lines 才是逐句的那份。
        if sc.startswith("[") and sc.endswith("]"):
            try:
                import ast
                parsed = ast.literal_eval(sc)
                if isinstance(parsed, (list, tuple)):
                    fixed = "\n".join(str(x).strip() for x in parsed if str(x).strip())
                    if fixed:
                        sc = fixed
            except Exception:
                pass
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

    # ★ 统一交给去重函数处理（2026-10-29 重写）。
    #   旧逻辑用「整行 == 整块落点」比较 —— 模型把落点拆成两行时一行都删不掉，
    #   最后又追加一次 → 观众连听两遍（实测老王 M01/M02 都中招）。
    #   新逻辑按归一化后的包含关系判定，拆行/不拆行都能认出来，
    #   另外兜一层「尾部任意段落重复」。
    lines, dedup_notes = dedupe_script(lines, final_landing)
    script = "\n".join(lines)
    landing_ok = bool(final_landing) and bool(lines) and _norm(lines[-1]) == _norm(final_landing)

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
        # ★ 模型写完我们替它改过的地方，必须让人看见（不是静默改稿）。
        "dedup_notes": dedup_notes,
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
    from vstudio import resolve_ip, script_text_path, dedupe_script_text, landing_path
    import datetime as _dt
    e = resolve_ip(ip)
    path = script_text_path(e["path"], setname)
    bak = None
    if os.path.exists(path) and open(path, encoding="utf-8").read().strip():
        bak = path + ".bak." + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        import shutil as _sh
        _sh.copy2(path, bak)
    # ★ 落盘前最后一道闸：重复的话进 TTS 会被念两遍。
    #   去重逻辑在 vstudio.dedupe_script_text（唯一真源），这里只管调用。
    text, notes = dedupe_script_text(result.get("script") or "", result.get("landing") or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    out = {"path": path, "backup": bak}
    if notes:
        out["dedup_notes"] = notes
    # ★ 把这一套自己的落点存下来（05_文案/<套次>/landing.md）。
    #   下次写同套的稿子时直接用它，货品词就不会串到别的套次去。
    lp = landing_path(e["path"], setname)
    land = str(result.get("landing") or "").strip()
    if land:
        try:
            old = ""
            if os.path.exists(lp):
                with open(lp, encoding="utf-8") as f:
                    old = f.read().strip()
            if old != land:
                with open(lp, "w", encoding="utf-8") as f:
                    f.write(land.rstrip() + "\n")
                out["landing_path"] = lp
        except Exception:
            pass
    return out


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


# ---------------------------------------------------------------- 音色语气
VOICE_SYSTEM = """你是 TTS 配音导演。给你一个人设，你要写一句**音色语气指令**。

这条指令会直接传给 BreezeTTS2 的 VoiceDirection 节点，决定这个人怎么说话。
它不是「角色设定」，是**给配音演员的调度令**。

要求：
1. 一句话，20-45 字。写清：什么音色 + 什么情绪 + 什么节奏 + 什么口音（如果有）。
2. 用**正向**描述（"语速偏慢""带一点笑意"），
   **不要用否定式**（不要写"不夸张""不喊口号""不要太快"）——
   TTS 模型对否定句的处理不可靠，写了反而会出来。
3. 必须能从人设里推出来。人设写"实在、利落"，就写"实在利落，语气平稳"，
   不要凭空加"幽默""活泼"。
4. 不要写具体语速数字（模型自己会判断），不要写音色名（如"男声""女声"）。
5. 如果人设里写了固定 INSTR（"永不改动"那句），就照着它写，只在它基础上补充细节。

返回 JSON：
{"instruction":"这句话","rationale":"为什么这么写，30字以内",
 "alternatives":["另外两个备选，各20-40字"]}"""


def design_voice_instruction(ip, prefer=None):
    """按人设 + 档案卡，设计一条音色语气指令。

    ★ 为什么需要这个：
      ip.json 的 voice.tts.instruction 是空的 → gen_tts.py 直接把空字符串
      传给 TTS（`INSTR = os.environ.get("INSTR") or IP.get(...) or ""`），
      **不会**自动填充，也不会报错 —— 片子照出，音色却是模型默认值，
      跟这个号的人设对不上。
      人设圣经里明明写着声音真值（珠珠那份就写死了「用热情友好、有活力的语气…」），
      但那句话得有人把它变成 TTS 认识的样子。
    """
    ctx = ip_context(ip)
    if not ctx["persona"].strip():
        raise RuntimeError(
            f"{ctx['name']} 的 01_人设与规范/ 里没有 md 材料。\n"
            "音色语气要从人设推 —— 没有人设就只能瞎编一个中性语调，"
            "那等于白设。\n先在界面的「人设与规范」区写一份，或用「AI 起草」。")
    import ip_policy
    cloud_why = ip_policy.require_cloud(ctx["root"], ctx["name"])
    card = ctx["card"]
    redlines = _texts(card.get("redlines"))
    habits = _texts(card.get("habits"))
    # 现在的音色指令（从 ip.json 真源读，别处再存一份）
    try:
        import json as _j
        with open(os.path.join(ctx["root"], "ip.json"), encoding="utf-8") as f:
            cur_instr = str((_j.load(f).get("voice") or {}).get("tts", {})
                            .get("instruction") or "").strip()
    except Exception:
        cur_instr = ""

    user = f"""# 这个 IP
名字：{ctx['name']}
类型：{card.get('type', '?')}    定位：{card.get('role', '')}

# 客户的需求
{json.dumps(card.get('needs') or {}, ensure_ascii=False, indent=1)}

# 说话习惯
{chr(10).join('- ' + h for h in habits) if habits else '（没记）'}

# 红线（音色也不能碰）
{chr(10).join('- ' + r for r in redlines) if redlines else '（没记）'}

# 人设材料（重点看「声音真值」「人格设定」两节）
{ctx['persona']}

# 现在的音色指令
{cur_instr or '（空 —— 这就是为什么要有这次设计）'}"""

    data, provider = chat_json(VOICE_SYSTEM, user, max_tokens=1200,
                                prefer=prefer, temperature=0.5)
    ins = str(data.get("instruction") or "").strip()
    if not ins:
        raise RuntimeError("模型没给出音色指令")
    # 否定式自查：TTS 对否定句处理不可靠
    negs = [w for w in ("不要", "不能", "别", "不许", "不得", "不是") if w in ins]
    return {
        "ip": ctx["name"], "provider": provider,
        "instruction": ins,
        "rationale": _short(data.get("rationale"), 60),
        "alternatives": [str(x).strip() for x in (data.get("alternatives") or [])][:3],
        "has_negative": negs,
        "cloud_policy": cloud_why,
    }


# ---------------------------------------------------------------- 人设圣经
PERSONA_SYSTEM = """你是短视频工作室的「人设圣经」撰写员。

客户会跟你说这个号是谁、干什么。**人设圣经是唯一真值** ——
写脚本、写提示词、跑出片之前都要看它，不许临时改。

你要写的是一份**能直接用**的人设圣经，结构固定（参考成熟 IP 的圣经，如「珠珠」那份）：

# <名字> · AI-IP 人设圣经
> 版本 v1.0 · <日期> · 用途：<平台/用途>
> 这份文档是唯一真值。写脚本、写提示词、跑出片之前先看这里，不要临时改。

## 一、一句话定位
（不是主播，不是老师，是什么身份。只讲 X、Y、Z，讲完让人敢做/敢来。
一句话说清「我是谁 + 我站在什么位置 + 观众能得到什么」。）

## 二、账号设定
表格：平台 / 账号名 / 简介 / 获客区域。**不写「全国」「不限地区」**，锁定具体地域。

## 三、目标人群
两到三层，优先级从高到低。每层写：是谁 + 核心痛点 + 最在意什么。
最后点明他们共同的痛点是什么。

## 四、人格设定
性格 / 价值观 / 关系姿态（是同行、供货方、还是老师？）/ 情绪基线 /
口头禅（3-5 条，自然带，不硬塞）/ **绝不做的姿态**（卖惨、贩卖焦虑、贬低同行、装专家）。

## 五、形象真值
脸 / 发型（逐项写死，最容易漂）/ 身形与穿搭 / 年龄感 / 场景 / 光线与机位。
⚠ 不能用「和参考图一样」带过 —— 必须把五官和发型结构全部**肯定式**写明。

## 六、声音真值
表格：音色 / 语速 / 情绪 / 口音 / 参考音频 / TTS 配方 / 固定 INSTR。
固定 INSTR 写「永不改动」。

## 七、口播结构
表格：时间段 / 段落 / 任务。再列规矩（用什么句式、禁什么句式、主体段不复述结尾）。

## 八、内容支柱
3-5 个系列。每个写：叫什么 / 解决什么 / 2-3 个具体选题例子 / 尺寸倾向。
★ 这一节决定「每天发什么」—— 没有它，AI 每次出的题都会飘。

## 九、钩子库
5-10 条开场白，可直接抄。
**结尾固定动作**（每条都一样）：写死那一句。

## 十、视觉规范
表格：横屏/竖屏的分辨率、构图、字幕字号、封面、片头、BGM。

## 十一、禁止项
分「业务禁区」和「技术禁区」两栏。技术禁区写渲染/提示词层面的坑。

## 十二、每周产出自检
5 个问题，发之前问自己。

=====
写作要求：
1. 用**客户自己的语气**写，不要写成广告文案，不要写成模板填空。
2. 客户没说的具体事实（数字、经历、地址）**不要编**。写不出来的写「待补」。
3. 篇幅 1200-2000 字。结构完整比字数多重要。
4. 用 markdown，表格用 markdown 表格。

返回 JSON：
{"persona_md":"完整正文（markdown）",
 "questions":["还需要问客户的关键问题，最多3条"],
 "summary":"你对这个 IP 的理解，一句话，40字以内"}"""


def draft_persona(text, prefer=None, ip_hint=""):
    """自然语言 → 人设圣经草案（markdown）。

    ★ 为什么单独做这件事：
      01_人设与规范/ 是空的 → AI 写稿时没东西可读 → 只能瞎编。
      以前唯一的办法是「对话式建档」顺手带出来，但那份是粗的（两篇几百字），
      缺了内容支柱/钩子库/禁止项这些**真正决定片子长什么样**的部分。
      现在按成熟 IP 的圣经结构写一份完整的。
    """
    if not (text or "").strip():
        raise RuntimeError("先说说这个号是谁、要做什么")
    user = f"""客户原话：

{text.strip()}

{f'补充：{ip_hint}' if ip_hint.strip() else ''}"""
    cloud_why = "用户主动提交，按新客户处理"
    if ip_hint.strip():
        try:
            from vstudio import resolve_ip
            e = resolve_ip(ip_hint)
        except SystemExit:
            e = None
        if e:
            import ip_policy
            cloud_why = ip_policy.require_cloud(e["path"], e["name"])
    data, provider = chat_json(PERSONA_SYSTEM, user, max_tokens=4000,
                                prefer=prefer, temperature=0.6)
    md = str(data.get("persona_md") or "").strip()
    if not md:
        raise RuntimeError("模型没给出正文，换个说法再试")
    return {
        "provider": provider,
        "persona_md": md,
        "questions": [str(q) for q in (data.get("questions") or [])][:3],
        "summary": _short(data.get("summary"), 60),
        "cloud_policy": cloud_why,
    }


# ---------------------------------------------------------------- 建档素材分析（形象卡）
# 建档时用户会把三样东西一起丢过来：人像参考图、音色参考音频、其它介绍文字。
# 这一段负责把它们变成能写进 01_人设与规范 的形象卡原料。
#
# ★ 关于「能不能识别音色」的实话：
#   目前路由里只有文本和图片两种输入，没有「听一遍音频就说得出音色」的通道。
#   硬要让模型描述音色，它只能编一段听起来很像的假话 —— 而后面 TTS 会照着这段
#   假描述去克隆，等于给一个真人安上不存在的嗓音。这比不给更糟。
#   所以这里只报仪器量得出来的数：时长 / 基频 / 语速 / 响度 / 静音占比，
#   再让 LLM 把这些数值翻译成一句人话（见 MATERIAL_VOICE_SYSTEM）。
#   测出来的东西不会编，编出来的东西害人。
MATERIAL_IMAGE_SYSTEM = """你是形象设计师，负责把一张人像参考图变成**可执行的形象真值**。

看图说话：只写画面上确实存在的东西。不要推测性格，不要评价好不好看。

输出（没把握的写「看不出」，不许编）：
  gender       性别呈现（男 / 女 / 看不出）
  age          年龄段（如 30-40 岁）
  face         脸型与五官（一句）
  skin         肤色与皮肤质感（一句，正向说法：哑光 / 细腻 / 偏深 …）
  hair         发型发色（一句）
  clothes      上装着装（一句：颜色 + 类型）
  frame        构图与机位（如 胸口以上中景、正面、轻微仰拍）
  light        光线（如 正面柔光、左侧主光）
  bg           背景（一句）
  vertical     是否竖屏（true / false）
  summary      整体气质（15 字以内）

★ 全中文。禁止否定式（不许写「不油」「不老」），一律改成正向说法。
只输出 JSON，不要 markdown 代码块。"""

MATERIAL_VOICE_SYSTEM = """你是配音导演。下面是一段参考音的**客观测量值**（仪器测出来的，不是主观印象）。

把它翻成两句人话：一句给 TTS 当指令，一句给人看。

要求：
1. instruction（给 TTS 的指令）：20-45 字，写清 音色 + 情绪 + 节奏。
   ★ 正向描述，**禁止否定词**（不许写「不快」「不夸张」）—— TTS 对否定句处理不可靠，
     写了反而会照着否定词出效果。
2. human（给人看的）：30 字以内，说清这个声音听起来是什么样。
3. caution：测量值里有异常（太短 / 太吵 / 静音太多）就提醒一句；没有就填空字符串。

只输出 JSON：{"instruction":"…","human":"…","caution":"…"}"""

MATERIAL_CARD_SYSTEM = """你是 AI 数字人 IP 的形象档案撰写人。

手上有：客户/本人的一段口语介绍、若干张人像参考图的画面分析、一段参考音的客观测量。
你要把它们合成一份**形象卡**：以后 AI 选题、写文案、生图生视频都要照着这份来。

写法规矩：
1. 只写**有依据**的内容，每条后面标来源：
   - 【介绍】来自客户口语介绍
   - 【图像】来自参考图的画面观察
   - 【声音】来自参考音的客观测量
   没有依据的一律不写，不要「合理推测」。
2. 结构用 markdown，分这几节：形象真值 / 声音真值 / 气质与表达 / 出镜与画面 / 待确认
3. 「待确认」必须写：把介绍里没说清、但出片必须知道的列出来（是否戴眼镜、常穿什么、
   口音强弱、能不能露脸等）。这是下次沟通的清单。
4. 不要写空话（「要真实可信」「要有感染力」这类一律删）。

只输出 markdown 正文，不要解释。"""

_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_AUD_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus"}
# 人声基频经验区间。只是参考刻度，最终以耳朵为准 —— 所以下面只报数不给结论。
_F0_MALE = (85, 155)
_F0_FEMALE = (165, 255)


def _ff():
    """ffmpeg 路径：环境变量 FFMPEG > config/pipeline.json > PATH。

    统一走这一个出口 —— 各处自己找，迟早有一处找到个不能用的。
    """
    import shutil as _sh
    p = os.environ.get("FFMPEG")
    if p and os.path.exists(p):
        return p
    try:
        with open(os.path.join(ROOT, "config", "pipeline.json"), encoding="utf-8") as f:
            ff = json.load(f).get("ffmpeg")
        if ff and os.path.exists(ff):
            return ff
    except Exception:
        pass
    return _sh.which("ffmpeg")


def probe_image(path):
    """读图片宽高。走 ffmpeg，不引 Pillow —— 项目保持零第三方依赖。"""
    ff = _ff()
    if not ff:
        return {}
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30)
        s = p.stderr.decode("utf-8", "ignore")
        m = re.search(r",\s*(\d{2,5})x(\d{2,5})", s)
        return {"w": int(m.group(1)), "h": int(m.group(2))} if m else {}
    except Exception:
        return {}


def _f0_median(frames, eng, peak, sr, lo=80, hi=300):
    """逐帧自相关估基频，取浊音帧中位数（Hz）。

    只算有声帧：静音段没有周期性，混进去只会把 F0 拉偏。
    帧长 50ms、不重叠 —— 精度够判断音区，计算量又压得住（纯 Python，无 numpy）。
    """
    lag_lo = max(2, int(sr / hi))
    lag_hi = min(int(sr / lo), len(frames[0]) - 2)
    if lag_hi <= lag_lo:
        return None
    got = []
    for fr, e in zip(frames, eng):
        if e <= peak * 0.12:
            continue
        m = sum(fr) / len(fr)
        d = [float(x) - m for x in fr]
        e0 = sum(x * x for x in d)
        if e0 <= 0:
            continue
        best, bl = 0.0, 0
        for lag in range(lag_lo, lag_hi + 1):
            s = 0.0
            for i in range(len(d) - lag):
                s += d[i] * d[i + lag]
            r = s / e0
            if r > best:
                best, bl = r, lag
        if best >= 0.35 and bl:
            got.append(sr / bl)
    if not got:
        return None
    got.sort()
    return round(got[len(got) // 2], 1)


def _syllable_rate(eng, peak, sr, fl):
    """用能量包络的「响起来又落下去」次数估音节率（语速代理，字/秒）。

    ★ 不是 ASR，给不出准确字数。这里数的是音节团，中文口语一字约一团，
      所以这个数 ≈ 语速。够用来判断快慢，别拿它当逐字稿。
    """
    if not eng or peak <= 0:
        return None
    hi, lo = peak * 0.35, peak * 0.15
    cnt, armed = 0, False
    for e in eng:
        if not armed and e > hi:
            armed, cnt = True, cnt + 1
        elif armed and e < lo:
            armed = False
    dur = len(eng) * fl / float(sr)
    if dur < 0.5:
        return None
    return round(cnt / dur, 2)


def _voice_advice(m):
    """按测量值给提醒。只说测得出来的异常，不评价声音好不好听。"""
    tips = []
    d = m.get("duration")
    if isinstance(d, (int, float)):
        if d < 5:
            tips.append("偏短（%.1fs），参考音建议 6-12 秒，太短克隆不稳" % d)
        elif d > 20:
            tips.append("偏长（%.1fs），参考音取 6-12 秒就够了，长段反而掺进杂语气" % d)
    db = m.get("loudness_dbfs")
    if isinstance(db, (int, float)):
        if db < -30:
            tips.append("整体偏轻（%.1f dBFS），先做响度归一化再当参考音" % db)
        elif db > -12:
            tips.append("整体偏响（%.1f dBFS），有削顶风险，先降增益" % db)
    sr = m.get("silence_ratio")
    if isinstance(sr, (int, float)) and sr > 0.5:
        tips.append("静音占了 %d%%，有效语音太少，建议重录一段连贯的话" % int(sr * 100))
    f0 = m.get("f0_hz")
    if isinstance(f0, (int, float)):
        if f0 < _F0_MALE[1]:
            tips.append("基频 %.0f Hz，落在男声常见区间" % f0)
        elif f0 > _F0_FEMALE[0]:
            tips.append("基频 %.0f Hz，落在女声常见区间" % f0)
        else:
            tips.append("基频 %.0f Hz，在男女声重叠区，以耳朵为准" % f0)
    return tips


def probe_audio(path):
    """测量一段参考音的客观特征。纯标准库 + ffmpeg，不需要 numpy。

    返回 {"ok": True, "metrics": {...}} 或 {"ok": False, "why": "…"}
    """
    ff = _ff()
    if not ff:
        return {"ok": False, "why": "找不到 ffmpeg（设环境变量 FFMPEG，或写进 config/pipeline.json 的 ffmpeg）"}
    if not os.path.exists(path):
        return {"ok": False, "why": "文件不在了，重传一次"}

    info = {}
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30)
        s = p.stderr.decode("utf-8", "ignore")
        d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", s)
        if d:
            info["duration"] = round(int(d.group(1)) * 3600
                                     + int(d.group(2)) * 60 + float(d.group(3)), 2)
        sr = re.search(r"(\d{3,6}) Hz", s)
        if sr:
            info["sample_rate"] = int(sr.group(1))
        ch = re.search(r"Audio:.*?,\s*(mono|stereo)", s)
        if ch:
            info["channels"] = ch.group(1)
    except Exception as ex:
        return {"ok": False, "why": "读不了这个文件：%s" % ex}

    try:
        p = subprocess.run([ff, "-v", "error", "-i", path, "-t", "8",
                            "-ar", "6000", "-ac", "1", "-f", "s16le",
                            "-c:a", "pcm_s16le", "-"],
                           capture_output=True, timeout=120)
        raw = p.stdout
    except Exception as ex:
        return {"ok": False, "why": "解码失败：%s" % ex}
    if len(raw) < 1200:
        return {"ok": False, "why": "音频太短，量不出东西（参考音建议 6-12 秒）"}

    smp = array.array("h")
    smp.frombytes(raw[:len(raw) // 2 * 2])
    sr = 6000
    fl = int(sr * 0.05)                       # 50ms 帧
    frames = [smp[i:i + fl] for i in range(0, max(1, len(smp) - fl), fl)]
    if not frames:
        return {"ok": False, "why": "音频太短"}

    eng = [(sum(float(x) * x for x in fr) / len(fr)) ** 0.5 for fr in frames]
    peak = max(eng) or 1.0
    voiced = [e for e in eng if e > peak * 0.12]
    silence = round(1 - len(voiced) / float(len(eng)), 3)
    rms = sum(eng) / len(eng)
    dbfs = round(20 * math.log10(rms / 32768.0), 1) if rms > 0 else -99.0

    info.update({
        "silence_ratio": silence,
        "loudness_dbfs": dbfs,
        "f0_hz": _f0_median(frames, eng, peak, sr),
        "syllables_per_sec": _syllable_rate(eng, peak, sr, fl),
    })
    info["advice"] = _voice_advice(info)
    return {"ok": True, "metrics": info}


def _vision_json(system, image_path, question, prefer=None, max_tokens=900):
    """走视觉通道问一张图。

    ★ sensitive=True：客户人像是商业形象资产，不进第三方免费池。
    """
    import base64
    load_env()
    from llm_router import LLMRouter
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = LLMRouter()
    d = r.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ]},
    ], max_tokens=max_tokens, temperature=0.3,
        sensitive=True, need="vision", prefer=prefer)
    content = (d["choices"][0]["message"].get("content") or "").strip()
    txt = content
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
    if m:
        txt = m.group(1)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise ValueError("模型没给 JSON：" + content[:300])
    return json.loads(m.group(0)), d.get("_provider", "?")


def analyze_materials(upload_dir, intro="", prefer=None):
    """分析建档暂存的素材，产出形象卡原料。

    upload_dir  .cache/uploads/<uid>/（用户刚传的图 / 音）
    intro       界面上补的「其它介绍」文字

    返回：
      images  [{file, w, h, desc{…}}]   视觉模型看图的结果（看不了就只留尺寸）
      audios  [{file, metrics{…}}]      仪器量出来的客观特征
      voice_instruction / voice_human   音色指令（有音频才有）
      image_card_md                     形象卡正文（图像 + 介绍合成）
      notes                             给界面显示的提醒
    """
    if not os.path.isdir(upload_dir):
        raise RuntimeError("暂存目录不在了，请重新上传")

    out = {"provider": "?", "images": [], "audios": [], "notes": [],
           "voice_instruction": "", "voice_human": "", "image_card_md": ""}

    files = sorted(os.listdir(upload_dir))
    imgs = [f for f in files if os.path.splitext(f)[1].lower() in _IMG_EXT]
    auds = [f for f in files if os.path.splitext(f)[1].lower() in _AUD_EXT]

    # ---- 图片：走视觉通道，逐张问 ----
    for f in imgs[:6]:                        # 最多看 6 张，够定形象了
        fp = os.path.join(upload_dir, f)
        item = {"file": f, "path": fp}
        item.update(probe_image(fp) or {})
        try:
            desc, prov = _vision_json(
                MATERIAL_IMAGE_SYSTEM, fp,
                "按系统提示里的字段描述这张人像参考图，只输出 JSON。")
            item["desc"] = desc
            out["provider"] = prov
        except Exception as ex:
            # 视觉通道不可用不该拖垮建档 —— 记一笔，让人自己补描述。
            item["desc"] = None
            out["notes"].append("「%s」没能看图（%s），形象描述要手填" % (f, ex))
        out["images"].append(item)

    # ---- 音频：只测物理量，不让模型听 ----
    for f in auds[:4]:
        fp = os.path.join(upload_dir, f)
        r = probe_audio(fp)
        out["audios"].append({"file": f, "path": fp,
                              "ok": r.get("ok"), "why": r.get("why", ""),
                              "metrics": r.get("metrics")})
        if not r.get("ok"):
            out["notes"].append("「%s」：%s" % (f, r.get("why")))

    # ---- 有音频 → 把测量值翻成音色指令 ----
    good = [a for a in out["audios"] if a.get("ok") and a.get("metrics")]
    if good:
        m = good[0]["metrics"]
        payload = json.dumps(m, ensure_ascii=False, indent=1)
        try:
            d, prov = chat_json(MATERIAL_VOICE_SYSTEM,
                                "参考音的客观测量值：\n\n" + payload + "\n\n翻成音色指令，只输出 JSON。",
                                max_tokens=800, prefer=prefer, temperature=0.4)
            out["voice_instruction"] = str(d.get("instruction") or "").strip()
            out["voice_human"] = str(d.get("human") or "").strip()
            out["provider"] = prov
            if str(d.get("caution") or "").strip():
                out["notes"].append(str(d["caution"]).strip())
        except Exception as ex:
            # 翻译失败也别丢测量值 —— 原始数值照样给出去，人能自己判断。
            out["notes"].append("音色指令没翻出来（%s），下面是原始测量值，可照着写" % ex)
            out["voice_human"] = "基频 %s Hz · 语速约 %s 音节/秒 · 响度 %s dBFS" % (
                m.get("f0_hz"), m.get("syllables_per_sec"), m.get("loudness_dbfs"))
    for t in (good[0]["metrics"].get("advice") or []) if good else []:
        out["notes"].append(t)

    # ---- 形象卡：图像观察 + 介绍文字合成 ----
    if imgs or (intro or "").strip():
        user = (
            "客户/本人的口语介绍：\n" + ((intro or "").strip() or "（没写）") + "\n\n"
            "人像参考图的画面分析：\n"
            + json.dumps([{"file": i["file"], "w": i.get("w"), "h": i.get("h"),
                           "desc": i.get("desc")} for i in out["images"]],
                         ensure_ascii=False, indent=1) + "\n\n"
            "参考音的客观测量：\n"
            + json.dumps([{"file": a["file"], "metrics": a.get("metrics")}
                          for a in out["audios"]], ensure_ascii=False, indent=1)
            + "\n\n按系统提示写形象卡，只输出 markdown 正文。"
        )
        try:
            md, prov = chat_text(MATERIAL_CARD_SYSTEM, user,
                                 max_tokens=2600, prefer=prefer, temperature=0.5)
            out["image_card_md"] = md
            out["provider"] = prov
        except Exception as ex:
            out["notes"].append("形象卡没写成（%s），素材已存下，可手动补" % ex)
    else:
        out["notes"].append("既没传图也没写介绍，形象卡跳过")

    return out


def chat_text(system, user, max_tokens=1500, prefer=None, temperature=0.6):
    """要一段自由文本（markdown）回来 —— 不是所有产出都适合塞进 JSON。"""
    load_env()
    from llm_router import LLMRouter
    r = LLMRouter()
    d = r.chat([{"role": "system", "content": system},
                {"role": "user", "content": user}],
               max_tokens=max_tokens, temperature=temperature,
               sensitive=True,          # ← 同文件头。这行不许改。
               prefer=prefer or None)
    return (d["choices"][0]["message"].get("content") or "").strip(), d.get("_provider", "?")


STILL_SYSTEM = """You write the image prompt used to generate a client's portrait for AI video.

The output goes straight into a text-to-image model and becomes the first frame of a
paid video. If it is vague, the client's face changes between videos. If anything is
wrong, the money is wasted.

HARD RULES:
1. English only. Not one Chinese character - the model renders stray Chinese as visible
   text burned into the picture.
2. Positive statements only. Never use no/not/never/without/avoid. To say "no glasses",
   write "He wears no glasses" -> WRONG; write "His face is unobstructed, his eyes clearly visible."
   The image model does not reliably obey negation, so describe what IS there.
3. Only things visible in a photo: face shape, hair, facial features, clothing, framing,
   lens, lighting, background objects. Never abstract praise ("professional", "trustworthy")
   - convert it into concrete visible detail.
4. Never ask for text, logos, watermarks, UI, or anything written on screen.
5. One English sentence per aspect, each ending with a period. Maximum 45 words total.
6. Always specify: vertical 9:16 framing, photographic realism.
7. Keep the person's identity STABLE and simple - this same prompt must reproduce the
   same recognizable person every time.

Return JSON only:
{"who_en": "...", "scene_en": "...", "prompt": "...", "notes": [...]}
 - who_en: the person only (face, hair, clothing, expression)
 - scene_en: background/environment only
 - prompt: the full sentence combining both, ready to submit
 - notes: 0-3 short Chinese notes for the human operator (what you inferred / what to double check)
"""

STILL_POLISH_SYSTEM = """You improve an existing image prompt for a text-to-image model.

Keep the described person RECOGNIZABLY THE SAME - operators reuse this across many
videos, so never change identity features you were told about. Improve clarity,
framing, lighting, and realism only.

HARD RULES:
1. English only, zero Chinese characters.
2. Positive statements only - no no/not/never/without/avoid.
3. Only visible facts. Describe what is there, never what should be absent.
4. No text, logo, watermark, or UI requests.
5. Vertical 9:16, photographic realism, sentences ending with a period, max 45 words.

Return JSON only:
{"prompt": "...", "changed": ["concrete sentence about what you improved"], "notes": [...]}
"""


def draft_still_prompt(ip, who, scene="", polish="", prefer=None):
    """定帧素材的提示词：从中文口语描述生成，或把现有提示词润色一遍。

    返回里带 `notes` 是有意的 —— AI 补出来的东西（比如它猜了这个人戴不戴眼镜）
    必须让人看得见，否则补错的细节会一路印到成片上去。
    """
    mode = "polish" if polish.strip() else "new"
    if mode == "polish":
        user = ("现有提示词：\n%s\n\n"
                "人物原本的描述（不得改变身份特征）：\n%s\n%s"
                % (polish.strip(), who.strip(),
                   ("背景要求：\n%s" % scene.strip()) if scene.strip() else ""))
        out, prov = chat_json(STILL_POLISH_SYSTEM, user, max_tokens=900,
                              prefer=prefer, temperature=0.5)
        out["prompt"] = str(out.get("prompt") or "").strip()
    else:
        user = ("这个人长什么样（客户原话）：\n%s\n%s"
                % (who.strip(),
                   ("背景/环境：\n%s" % scene.strip()) if scene.strip() else "背景没说，按他的身份合理补一个。"))
        out, prov = chat_json(STILL_SYSTEM, user, max_tokens=900,
                              prefer=prefer, temperature=0.6)
        out["who_en"] = str(out.get("who_en") or "").strip()
        out["scene_en"] = str(out.get("scene_en") or "").strip()
        out["prompt"] = str(out.get("prompt") or "").strip()

    notes = [str(x).strip() for x in (out.get("notes") or []) if str(x).strip()][:5]
    out["notes"] = notes
    out["mode"] = mode
    out["ip"] = ip
    out["provider"] = prov

    # 护栏：定帧提示词里出现汉字 = 会被烧成画面上的字，和出片提示词同一个坑。
    # 这里不静默改，直接报错让人看见（改了内容但没告知，比报错更糟）。
    p = out.get("prompt") or ""
    if re.search(r"[\u4e00-\u9fff]", p):
        raise ValueError("AI 写的定帧提示词里混进了中文，已拦下（原文：%s）" % p[:120])
    if re.search(r"\b(no|not|never|without|avoid)\b", p, re.I):
        raise ValueError("定帧提示词里出现否定词，模型会听反（原文：%s）" % p[:120])
    return out


# ---------------------------------------------------------------- CLI
# ══════════════════════════════════════ 素材：AI 自动打标 + 按段匹配
#
# 用户的原话是「我提供所有素材，AI 自行调用和匹配」。所以这里不做逐段手勾，
# 拆成两件事：
#   ① 逐张看图写标签 —— 只做一次，结果**缓存进 materials.json**，
#      之后重跑不再花钱重看（视觉调用是这里最贵的一步）。
#   ② 拿每段的文案分镜 + 素材标签，让模型一次性把素材分到各段，
#      并且**必须为每一段给出一句理由** —— 人看不见理由就不会信这个分配。
#
# ★ 只输出 JSON，不输出 markdown 代码块（_vision_json 会尝试剥，但别依赖）。

ASSET_TAG_SYSTEM = """你是素材管理员，给一张视频素材图写检索用的标签。

这可能是人像，也可能是门店、产品、场景、空镜 —— 别一律当人像写。

看图说话，只写画面上确实存在的东西。没把握写「看不出」，不许编。

输出 JSON：
  subject       画面主体（一句，如「穿白色对襟衫的中年男性」/「火锅店灶台」）
  scene         环境背景（一句）
  composition   构图与机位（如 胸口以上中景、正面、轻微仰拍）
  light         光线（如 正面柔光、暖色射灯）
  mood          氛围（如 干净通透、烟火气、安静）
  is_portrait   是否人物主体（true / false）
  usable_for    适合用作什么（一句，如「人物出镜的定帧」「门店空镜」）
  summary       15 字以内概括

★ 全中文。禁止否定式（不许写「不油」「不暗」），一律改成正向说法。
只输出 JSON，不要 markdown 代码块。"""

ASSET_MATCH_SYSTEM = """你是剪辑师，把客户给的素材分配到视频的每一段。

输入：① 每段的台词与画面描述 ② 素材清单（每张有 AI 看图写出的标签）

分配规则（按重要性）：
1. 这段在讲什么，就配什么。讲门店配门店照，讲产品配产品照。
2. 人物口播段优先配本人素材；只有这段明确在讲别的东西时才换。
3. ★ 没有合适素材的段，一律配人物主素材（清单里标记 _primary 的那张），
   不要留空 —— 段里没有参考图会让脸直接崩掉。
4. 一段最多 3 张，按主次排序（第一张是这段的主画面）。
5. 别为了「每段都不一样」硬换。连续几段用同一张是常态。

输出 JSON：
{
  "segments": {"1": ["id1"], "2": ["id2"], ...},
  "reasons":  {"1": "一句话说明为什么这么配", ...}
}
段号写成字符串键。只输出 JSON，不要 markdown 代码块。"""

# 套次级素材目录用的是 jid（套次号，如 M01），不是完整套次名
_ASSET_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_PRIMARY_ID = "img"          # 起始静帧的固定 id，跟 gen_video 的兜底一致


def _full_set(root, setname):
    """把用户给的套次（可能是简写）补全成 05_文案 下的真实目录名。"""
    d05 = os.path.join(root, "05_文案")
    if os.path.isdir(os.path.join(d05, setname)):
        return setname
    if os.path.isdir(d05):
        for n in sorted(os.listdir(d05)):
            if n == setname or n.startswith(setname):
                return n
    return setname


def _jid(setname):
    m = re.match(r"^([A-Za-z]+\d+)", setname)
    return m.group(1) if m else setname


def tag_asset(path, prefer=None):
    """给一张素材图写标签。走视觉通道（sensitive=True，不进免费池）。"""
    return _vision_json(
        ASSET_TAG_SYSTEM, path,
        "按系统提示里的字段描述这张素材，只输出 JSON。", prefer=prefer)


def match_materials(ip, setname, prefer=None, force_retag=False, apply=True):
    """扫描本套素材 → 逐张打标 → 按段匹配 → 写回 materials.json。

    返回 {images, segments, reasons, provider, notes, materials_path, tagged}
    """
    from vstudio import resolve_ip
    e = resolve_ip(ip)
    root = e["path"]
    full = _full_set(root, setname)
    jid = _jid(full)

    mdir = os.path.join(root, "02_素材真值", jid)
    mp = os.path.join(root, "05_文案", full, "materials.json")
    pj = os.path.join(root, "05_文案", full, "prompts.json")

    out = {"ip": e["name"], "set": full, "dir": mdir, "materials_path": mp,
           "images": [], "segments": {}, "reasons": {}, "notes": [],
           "provider": "?", "tagged": 0}

    # ---- ① 收集素材：套次级优先，IP 级兜底 ----
    doc = {}
    if os.path.isfile(mp):
        try:
            doc = json.load(open(mp, encoding="utf-8")) or {}
        except Exception:
            doc = {}
    known = {x.get("id"): x for x in (doc.get("images") or []) if x.get("id")}

    def _add(file_rel, name, primary=False):
        it = dict(known.get(_id_of(file_rel, primary), {}))
        it.update({"id": _id_of(file_rel, primary), "file": file_rel, "name": name})
        if primary:
            it["_primary"] = True
        out["images"].append(it)

    def _id_of(file_rel, primary):
        return _PRIMARY_ID if primary else ("a%d" % len(out["images"]))

    # 起始静帧（永远在，作为所有段的兜底 —— 见规则 3）
    still = _read_ip_still(root)
    if still:
        it = dict(known.get(_PRIMARY_ID, {}))
        it.update({"id": _PRIMARY_ID, "file": still,
                   "name": os.path.basename(still), "_primary": True})
        out["images"].append(it)

    # 套次级素材
    if os.path.isdir(mdir):
        for n in sorted(os.listdir(mdir)):
            p = os.path.join(mdir, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in _ASSET_EXT:
                rel = os.path.join("02_素材真值", jid, n).replace("\\", "/")
                dup = [x for x in out["images"] if x["file"] == rel]
                if not dup:
                    _add(rel, n)

    if len(out["images"]) <= 1:
        out["notes"].append(
            "本套素材目录里还没有图（%s）。先把客户给的图传进去，再点自动分配。" % mdir)
        return out

    # ---- ② 逐张打标（有缓存就跳过，视觉调用很贵）----
    for it in out["images"]:
        if not force_retag and it.get("desc"):
            continue
        fp = os.path.join(root, it["file"])
        if not os.path.isfile(fp):
            it["desc"] = None
            out["notes"].append("「%s」找不到了，跳过打标" % it["name"])
            continue
        try:
            desc, prov = tag_asset(fp, prefer)
            it["desc"] = desc
            out["provider"] = prov
            out["tagged"] += 1
        except Exception as ex:
            it["desc"] = None
            out["notes"].append("「%s」没能看图（%s）—— 它还能用，只是按文件名匹配"
                                % (it["name"], ex))

    # ---- ③ 读分段 ----
    segs = []
    if os.path.isfile(pj):
        try:
            segs = (json.load(open(pj, encoding="utf-8")) or {}).get("segments") or []
        except Exception:
            segs = []
    if not segs:
        out["notes"].append("还没有分镜（prompts.json），先去「提示词」生成一次，"
                            "否则 AI 不知道每段在讲什么。")
        _save(root, mp, doc, out, apply)
        return out

    # ---- ④ 一次调用，按段匹配 ----
    asset_lines = []
    for it in out["images"]:
        d = it.get("desc") or {}
        if d:
            line = ("- id=%s | %s | 主体：%s | 环境：%s | 适合：%s"
                    % (it["id"], it["name"], d.get("subject", "看不出"),
                       d.get("scene", "看不出"), d.get("usable_for", "")))
        else:
            line = "- id=%s | %s |（没能看图，只能按文件名猜）" % (it["id"], it["name"])
        if it.get("_primary"):
            line += "  ★这是本人主素材，没别的合适就用它"
        asset_lines.append(line)

    seg_lines = []
    for s in segs:
        seg_lines.append("[段%s] 台词：%s\n     画面：%s"
                         % (s.get("i"), (s.get("line") or "")[:60],
                            (s.get("prompt") or s.get("line") or "")[:200]))

    question = ("下面是这条片子的分段：\n%s\n\n下面是可用素材：\n%s\n\n"
                "请按规则把素材分配到每一段，并给每段一句理由。"
                % ("\n".join(seg_lines), "\n".join(asset_lines)))
    try:
        # chat_json 返回的是 (data, provider) 元组 —— 不是 dict。
        # 2026-10-30 第一次写这里时当 dict 用了，直接 AttributeError。
        d, prov = chat_json(ASSET_MATCH_SYSTEM, question, prefer=prefer,
                            max_tokens=1600, temperature=0.3)
        out["provider"] = prov
    except Exception as ex:
        out["notes"].append("匹配失败：%s" % ex)
        _save(root, mp, doc, out, apply)
        return out

    ids = {x["id"] for x in out["images"]}
    seg_map = {}
    reasons = {}
    for k, v in (d.get("segments") or {}).items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        picks = [p for p in (v if isinstance(v, list) else [v]) if p in ids]
        if not picks:                       # 模型给了不存在的 id → 兜底主素材
            picks = [_PRIMARY_ID] if _PRIMARY_ID in ids else []
        if picks:
            seg_map[str(n)] = picks[:3]
    for k, v in (d.get("reasons") or {}).items():
        try:
            reasons[str(int(k))] = str(v)
        except (TypeError, ValueError):
            pass

    # 模型漏掉的段，一律兜底主素材 —— 空段会让脸崩
    for s in segs:
        key = str(s.get("i"))
        if key not in seg_map and _PRIMARY_ID in ids:
            seg_map[key] = [_PRIMARY_ID]
            reasons.setdefault(key, "AI 没给这一段，自动用本人主素材兜底")

    out["segments"] = seg_map
    out["reasons"] = reasons

    # ---- ⑤ 用不上的素材要点名说清楚 ----
    # 2026-10-30 实测：给的门店/汤锅/牛肉三张，AI 一张都没用，全部分配给本人。
    #   这不是 bug，是分镜决定的：prompts.json 里每一段都写成「人物站在…」，
    #   都是人物口播段，按规则 2 就该用本人。
    #   但用户看到「素材白给了」会以为分配坏了 —— 所以必须把原因摆出来，
    #   并告诉他真正该怎么让素材进片子（改分镜，不是改这里）。
    used = set()
    for v in seg_map.values():
        used.update(v)
    unused = [it for it in out["images"]
              if it["id"] not in used and not it.get("_primary")]
    if unused:
        out["notes"].append(
            "有 %d 张素材没被用上（%s）。原因是每一段的分镜都写成「人物口播」，"
            "按规则口播段优先用本人。要让它们真的进片子，去「提示词」里给对应那一段"
            "加一句画面描述（比如「镜头切到翻滚的汤锅」），或者在下面手动指定。"
            % (len(unused), "、".join(x["name"] for x in unused[:4])))

    # ---- ⑥ 写回 ----
    _save(root, mp, doc, out, apply)
    return out


def _read_ip_still(root):
    """IP 的起始静帧路径（相对 IP 根）。所有段的兜底素材。"""
    try:
        d = json.load(open(os.path.join(root, "ip.json"), encoding="utf-8")) or {}
    except Exception:
        return ""
    rel = ((d.get("visual") or {}).get("still") or "").strip()
    if not rel:
        return ""
    if os.path.isabs(rel) or rel.startswith(".."):
        return ""
    return rel.replace("\\", "/")


def _save(root, mp, doc, out, apply):
    """把打标结果和分配结果写回 materials.json。"""
    # ★ 起始静帧不能写进清单：gen_video 里 BASE_IMAGE_ID 就是「img」，
    #   它由 ip.json 的 visual.still 提供，不需要（也不允许）在 materials.json 里登记
    #   —— 写了会撞名，gen_video 直接 exit：「id 不能叫 img」。
    #   但它在匹配阶段必须留在候选里，那是所有段的兜底，所以只在写回时排除。
    doc["images"] = [{k: v for k, v in it.items() if not k.startswith("_")}
                     for it in out["images"] if not it.get("_primary")]
    doc["segments"] = out.get("segments") or {}
    # ★ notes 也要存：最要紧的一条是「有 N 张素材没被用上，因为每段都写成口播」。
    #   这话只在分配那一刻说一次，不存下来人刷新页面就再也看不到了，
    #   于是就会以为「AI 把我素材弄丢了」。
    doc["_ai"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "provider": out.get("provider", "?"),
                  "reasons": out.get("reasons") or {},
                  "notes": out.get("notes") or []}
    if apply:
        os.makedirs(os.path.dirname(mp), exist_ok=True)
        json.dump(doc, open(mp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)


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

    dp = sub.add_parser("persona-draft", help="自然语言 → 人设圣经草案（markdown）")
    dp.add_argument("text")
    dp.add_argument("--prefer", default=None)
    dp.add_argument("--ip-hint", default="")
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--out", default="")

    st = sub.add_parser("still-prompt",
                        help="定帧素材：中文描述 → 英文出图提示词（也可润色现有提示词）")
    st.add_argument("ip")
    st.add_argument("--who", default="", help="这个人长什么样（中文口语即可）")
    st.add_argument("--scene", default="", help="背景/环境（中文即可）")
    st.add_argument("--polish", default="", help="给了就是「润色现有提示词」模式")
    st.add_argument("--prefer", default=None)
    st.add_argument("--json", action="store_true")
    st.add_argument("--out", default="")

    dv = sub.add_parser("voice", help="按人设设计音色语气指令")
    dv.add_argument("ip")
    dv.add_argument("--prefer", default=None)
    dv.add_argument("--json", action="store_true")
    dv.add_argument("--out", default="")

    am = sub.add_parser(
        "assets",
        help="本套素材：AI 逐张看图打标，再按文案分镜自动匹配到每段")
    am.add_argument("ip")
    am.add_argument("set")
    am.add_argument("--prefer", default=None)
    am.add_argument("--retag", action="store_true",
                    help="已经打过标的也重看一遍（默认复用缓存，省视觉调用的钱）")
    am.add_argument("--dry", action="store_true", help="只算不写回")
    am.add_argument("--json", action="store_true")
    am.add_argument("--out", default="")

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
        elif a.cmd == "still-prompt":
            if not (a.who or "").strip() and not (a.polish or "").strip():
                raise ValueError("要么给 --who（中文描述这个人），要么给 --polish（现有提示词）")
            r = draft_still_prompt(a.ip, a.who, a.scene, a.polish, a.prefer)
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(f"通道 {r.get('provider', '?')}   模式 {r['mode']}")
            print("\n英文提示词：")
            print("  " + (r["prompt"] or "（空）"))
            if r.get("who_en"):
                print("\n人物：" + r["who_en"])
            if r.get("scene_en"):
                print("背景：" + r["scene_en"])
            if r.get("changed"):
                print("\n改了什么：")
                for c in r["changed"]:
                    print("  - " + c)
            if r["notes"]:
                print("\n提醒：")
                for n in r["notes"]:
                    print("  - " + n)
        elif a.cmd == "persona":
            ctx = ip_context(a.ip)
            print(f"{ctx['name']}：{len(ctx['persona'])} 字，"
                  f"来自 {ctx['persona_files'] or '（空）'}")
            print(f"已有套次 {len(ctx['titles'])} 个")
        elif a.cmd == "persona-draft":
            r = draft_persona(a.text, a.prefer, a.ip_hint)
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(f"通道 {r['provider']}")
            print("=" * 56)
            print(r["persona_md"])
            print("=" * 56)
            if r["questions"]:
                print("还要问客户：")
                for q in r["questions"]:
                    print("  · " + q)
            print(f"理解：{r['summary']}")
        elif a.cmd == "voice":
            r = design_voice_instruction(a.ip, a.prefer)
            emit(r)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(f"通道 {r['provider']}")
            print(f"音色语气指令：{r['instruction']}")
            print(f"理由：{r['rationale']}")
            for i, alt in enumerate(r["alternatives"], 1):
                print(f"  备选{i}：{alt}")
            if r["has_negative"]:
                print(f"⚠ 含否定词 {r['has_negative']} —— TTS 对否定句处理不可靠，建议改成正向描述")
        elif a.cmd == "assets":
            r = match_materials(a.ip, a.set, prefer=a.prefer,
                                force_retag=a.retag, apply=not a.dry)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
                return
            print(f"套次 {r['set']}  通道 {r['provider']}  新打标 {r['tagged']} 张")
            print(f"素材目录 {r['dir']}")
            print("=" * 62)
            for it in r["images"]:
                d = it.get("desc") or {}
                mark = "★主" if it.get("_primary") else "  "
                sum_ = d.get("summary") or d.get("subject") or "（没能看图）"
                print(f" {mark} {it['id']:6s} {it['name'][:34]:34s} {sum_}")
            print("=" * 62)
            for k in sorted(r["segments"], key=lambda x: int(x)):
                print(f"  段{k:>2s} → {' + '.join(r['segments'][k])}")
                if r["reasons"].get(k):
                    print(f"        {r['reasons'][k]}")
            if r["notes"]:
                print("\n提醒：")
                for n in r["notes"]:
                    print("  - " + n)
            if a.dry:
                print("\n（--dry：没写回 materials.json）")
    except Exception as ex:
        print(f"✗ {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
