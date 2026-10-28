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
    redlines = card.get("redlines") or []
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
