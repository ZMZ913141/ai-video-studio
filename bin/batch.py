#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量出片 —— 一份队列，串行出多条片子。

为什么必须串行
==============
这台机器只有一块 4080S。并行提交的后果不是「快一倍」，是：
  · ComfyUI 把两个任务排进同一个队列，第二个等第一个跑完才开始 —— 并不会更快；
  · 更糟的是 --high-ram / --async-offload 2 这个组合会把权重留在 RAM，
    两个任务叠在一起时内存峰值翻倍，直接撞内核 oom-killer，
    **被杀时一个字节都写不出来**（日志断在半句、没有 Traceback，就这个特征）。
所以批量 = 排队，不是并发。这里用 subprocess 一条一条调 `vstudio.py pipeline`。

为什么要用子进程而不是直接调 Pipeline 类
==========================================
`pipeline.main()` 结尾是 `sys.exit()`。同进程连跑十条，第一条就把批处理本身退出了。
子进程还额外买到两个好处：每条的内存/显存完全隔离（跑十条不会越跑越胖），
以及某条崩了不会带崩剩下的。

每一条之间做什么
================
1. 资源预检（`bin/comfy_guard.py preflight`）—— 跑完一条后 ComfyUI 未必把模型卸干净，
   下一条直接提交就可能撞 OOM。预检不过会先自动 free 再复检，仍不过就**停下整个批次**，
   因为继续跑只会一条接一条被杀。
2. 文案：`topic` 就走 AI 现写（会花 LLM 调用），`script` 就用现成文件。
3. 建套次目录 + job.json，跟 `vstudio.py job-new` 完全同一套逻辑。

队列文件格式（JSON）
====================
```json
{
  "ip": "陈洁斌主号",
  "defaults": {"tier": "std"},
  "items": [
    {"set": "S06", "title": "为什么我不用真人出镜", "topic": "AI 分身和真人出镜的区别"},
    {"set": "S07", "script": "/path/to/已经写好的文案.txt"},
    {"set": "S08", "topic": "一条片子多少钱", "tier": "fast"}
  ]
}
```
`set` 可以不写 —— 那就按 IP 现有最大编号往后顺排（S05 之后就是 S06）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vstudio as V          # noqa: E402  复用「认 IP / 建套次 / 扫产物」那一套，不另写一份

PY = sys.executable


# ---------------- 队列 ----------------
def load_queue(path):
    """读队列文件。校验放在最前面 —— 跑到第 7 条才发现第 8 条缺 set，前面 6 条白跑。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        raw = {"items": raw}
    items = raw.get("items") or []
    if not items:
        raise SystemExit(f"✗ 队列文件里没有 items：{path}")

    bad = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            bad.append(f"第 {i} 项不是对象")
            continue
        if not it.get("set") and not it.get("topic") and not it.get("script"):
            bad.append(f"第 {i} 项三样全空（set / topic / script 至少要有一个）")
        if it.get("script") and not os.path.isfile(it["script"]):
            bad.append(f"第 {i} 项文案文件不存在：{it['script']}")
    if bad:
        raise SystemExit("✗ 队列有问题，一条都还没跑：\n  " + "\n  ".join(bad))
    return raw, items


def next_set_no(root, tag):
    """现有最大编号 +1。S05 → S06；M02 → M03。"""
    nums = []
    for s in V.list_sets(root):
        m = re.match(rf"^{re.escape(tag)}(\d+)", s)
        if m:
            nums.append(int(m.group(1)))
    return f"{tag}{(max(nums) + 1) if nums else 1:02d}"


def build_items_from_topics(root, tag, topics, sets, title_from_topic=True):
    """--topics "A;B;C" 的快捷路径：给每个选题配一个套次号。"""
    out = []
    for i, t in enumerate(topics):
        t = t.strip()
        if not t:
            continue
        setname = (sets[i].strip() if i < len(sets) else "")
        out.append({"set": setname, "topic": t, "_auto_no": not setname})
    return out


# ---------------- 单条 ----------------
def ensure_job(e, setname):
    """建套次目录 + job.json。跟 vstudio job-new 走同一批函数，不复制一套逻辑。"""
    dirs = V.set_dirs(e["path"], setname)
    for p in dirs.values():
        os.makedirs(p, exist_ok=True)
    V.build_job(e, setname, write=True)
    return dirs


def prepare_script(ip, setname, topic, script_path, tone=""):
    """把这套的文案准备好。返回 (ok, 说明)。

    ★ 已经有人写过文案（05_文案 里有文件）且这次没给 topic → 不动它。
      批量重跑时不覆盖人写的稿子，是最容易踩的一条：批量脚本一覆盖，
      昨天改半天的稿子就没了。
    """
    d05 = V.set_dirs(V.resolve_ip(ip)["path"], setname)["05"]
    existing = [f for f in os.listdir(d05)
                if f.endswith(".txt") and not f.startswith("job")] if os.path.isdir(d05) else []

    if script_path:
        # ★ 走共享函数，跟写稿方（ui_server / studio_ai）用同一个命名规则。
        #   两边各写各的 → 批量出片找不到稿子（2026-10-29 实测踩过）。
        dst = V.script_text_path(V.resolve_ip(ip)["path"], setname)
        if os.path.abspath(script_path) != os.path.abspath(dst):
            with open(script_path, encoding="utf-8") as f:
                txt = f.read()
            with open(dst, "w", encoding="utf-8") as f:
                f.write(txt)
        return True, f"用现成文案 → {os.path.basename(dst)}"

    if not topic:
        if existing:
            return True, f"沿用已有文案 {existing[0]}"
        return False, "没有文案：没给 topic 也没给 script"

    if existing:
        return True, f"已有文案 {existing[0]}（没给 topic，不覆盖）"

    cmd = [PY, os.path.join(HERE, "studio_ai.py"), "script", ip,
           "--set", setname, "--topic", topic, "--write", "--json"]
    if tone:
        cmd += ["--tone", tone]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"AI 写文案失败：{(r.stderr or r.stdout).strip().splitlines()[-1][:160]}"
    try:
        d = json.loads(r.stdout)
    except Exception:
        return False, "AI 写文案返回的不是 JSON，落盘情况不明"
    c = d.get("checks") or {}
    if not c.get("pass", True):
        # 不合格也留着，但必须说清楚 —— 悄悄放过才是害人
        return True, (f"文案已写但**没过自检**：{'；'.join(c.get('problems') or [])[:120]}"
                      f"（{c.get('chars')} 字 ≈ {c.get('est_secs')} 秒），建议改完再出片")
    return True, f"AI 写文案：{c.get('chars')} 字 ≈ {c.get('est_secs')} 秒（{d.get('provider')}）"


def preflight():
    """出片前资源预检。返回 (ok, 一行说明)。"""
    g = os.path.join(HERE, "comfy_guard.py")
    if not os.path.isfile(g):
        return True, "（没装 comfy_guard，跳过预检）"
    r = subprocess.run([PY, g, "preflight"], capture_output=True, text=True)
    tail = [x for x in (r.stdout or "").strip().splitlines() if x.strip()]
    return r.returncode == 0, (tail[-1][:150] if tail else "预检失败（无输出）")


def run_one(ip, setname, tier, extra, log_dir):
    """跑一条。返回 (ok, 摘要)。"""
    cmd = [PY, os.path.join(HERE, "vstudio.py"), "pipeline", ip, setname]
    if tier:
        cmd += ["--tier", tier]
    cmd += extra
    t0 = time.time()
    # stdout 直接继承：一条要跑十几分钟，用户得能看见它活着
    r = subprocess.run(cmd)
    sec = int(time.time() - t0)
    return r.returncode == 0, f"{sec // 60} 分 {sec % 60} 秒，退出码 {r.returncode}"


# ---------------- 主流程 ----------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="batch",
        description="批量出片：一份队列串行出多条（GPU 独占，不能并行）")
    ap.add_argument("ip", nargs="?", default="")
    ap.add_argument("--queue", help="队列文件（JSON），格式见 bin/batch.py 顶部")
    ap.add_argument("--topics", default="",
                    help="快捷方式：分号分隔的选题，'甲;乙;丙'")
    ap.add_argument("--sets", default="",
                    help="配 --topics 用，逗号分隔的套次名；不给就顺排编号")
    ap.add_argument("--tier", default="", help="档位 fast/pilot/std/high（队列里可逐条覆盖）")
    ap.add_argument("--from", dest="start", default="", help="从哪步开始")
    ap.add_argument("--to", dest="end", default="", help="到哪步结束")
    ap.add_argument("--dry", action="store_true", help="只算计划，不真跑（不花 GPU）")
    ap.add_argument("--force", action="store_true", help="每条都整链重做")
    ap.add_argument("--stop-on-error", action="store_true",
                    help="某条失败就停（默认继续跑剩下的）")
    ap.add_argument("--no-guard", action="store_true", help="跳过条间资源预检")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（试跑用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    # 被管道接住时 print 会攒着不发，子进程的输出反而先出来 —— 日志顺序全乱，
    # 「哪一行是哪一条的」就看不出来了。行缓冲解决它。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # ---- 队列来源 ----
    if a.queue:
        raw, items = load_queue(a.queue)
        ip = a.ip or raw.get("ip") or ""
        defaults = raw.get("defaults") or {}
    elif a.topics:
        if not a.ip:
            raise SystemExit("✗ 用 --topics 必须给 IP")
        ip = a.ip
        raw = {}
        defaults = {}
        cli_sets = [s for s in a.sets.split(",") if s.strip()] if a.sets else []
        items = [{"topic": t.strip(), "set": (cli_sets[i] if i < len(cli_sets) else "")}
                 for i, t in enumerate(a.topics.split(";")) if t.strip()]
    else:
        raise SystemExit("✗ 要么 --queue 队列文件，要么 --topics '甲;乙;丙'")

    if not ip:
        raise SystemExit("✗ 没有 IP：队列文件里写 ip，或者命令行上给")
    e = V.resolve_ip(ip)
    root = e["path"]
    tag = e.get("tag", "S")

    if a.limit:
        items = items[:a.limit]

    # ---- 先补全套次名（顺排编号要一次性算好，否则相邻两条会撞同一个号）----
    used = set(V.list_sets(root))
    cursor = next_set_no(root, tag)
    for it in items:
        if not it.get("set"):
            while cursor in used:
                m = re.match(rf"^{re.escape(tag)}(\d+)$", cursor)
                cursor = f"{tag}{int(m.group(1)) + 1:02d}" if m else cursor + "x"
            it["set"] = cursor
            m = re.match(rf"^{re.escape(tag)}(\d+)$", cursor)
            cursor = f"{tag}{int(m.group(1)) + 1:02d}" if m else cursor
        it["set"] = str(it["set"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"\n批量出片 · {e['name']} · {len(items)} 条 · 串行")
    print(f"  档位 {a.tier or defaults.get('tier') or '（配置默认）'}"
          f"{'  [DRY 只算计划]' if a.dry else ''}")
    for i, it in enumerate(items, 1):
        print(f"  {i}. {it['set']:<28} {(it.get('topic') or it.get('script') or '')[:44]}")

    # ---- 逐条 ----
    results = []
    t_all = time.time()
    for i, it in enumerate(items, 1):
        setname = it["set"]
        title = (it.get("title") or "").strip()
        print(f"\n{'═' * 62}\n[{i}/{len(items)}] {setname}"
              + (f"  {title}" if title else "") + f"\n{'═' * 62}")

        rec = {"set": setname, "title": title, "ok": False, "at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        # 1) 条间资源预检（第一条之前也做 —— 上次跑完可能模型还在）
        if not a.no_guard and not a.dry:
            ok, msg = preflight()
            print(f"  资源预检 {'✓' if ok else '✗'} {msg}")
            if not ok:
                rec["error"] = f"资源不足，批次中止：{msg}"
                results.append(rec)
                print(f"  ✗ 资源不够，不硬上（继续跑只会一条接一条被内核杀）")
                break

        # 2) 建套次
        if not a.dry:
            try:
                if setname not in V.list_sets(root):
                    ensure_job(e, setname)
                    print(f"  ✓ 建套 {setname}")
            except Exception as ex:
                rec["error"] = f"建套失败：{ex}"
                results.append(rec)
                if a.stop_on_error:
                    break
                continue

        # 3) 文案
        if not a.dry:
            ok, msg = prepare_script(ip, setname, (it.get("topic") or "").strip(),
                                     (it.get("script") or "").strip(),
                                     (it.get("tone") or "").strip())
            print(f"  文案 {'✓' if ok else '✗'} {msg}")
            if not ok:
                rec["error"] = msg
                results.append(rec)
                if a.stop_on_error:
                    break
                continue
            rec["script"] = msg

        # 4) 出片
        if a.dry and setname not in V.list_sets(root):
            # dry 不建目录，pipeline 会以「没有这套」退出 —— 那不是失败，
            # 是「这条真跑时会新建」。别让预演看起来像出错。
            print(f"  · 这条还没有套次，真跑时会新建 {setname}（dry 不建目录，跳过）")
            rec["ok"] = True
            rec["detail"] = "dry：会新建套次"
            results.append(rec)
            continue

        extra = []
        tier = it.get("tier") or a.tier or defaults.get("tier") or ""
        if a.start:
            extra += ["--from", a.start]
        if a.end:
            extra += ["--to", a.end]
        if a.dry:
            extra.append("--dry")
        if a.force:
            extra.append("--force")
        if it.get("seed"):
            extra += ["--seed", str(it["seed"])]
        if it.get("reseed"):
            extra.append("--reseed")

        ok, msg = run_one(ip, setname, tier, extra, None)
        rec["ok"] = ok
        rec["detail"] = msg
        results.append(rec)
        print(f"  {'✓' if ok else '✗'} {setname} {msg}")
        if not ok and a.stop_on_error:
            print("  （--stop-on-error：停下，后面的不跑了）")
            break

    # ---- 汇总 ----
    total = int(time.time() - t_all)
    good = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    skipped = [it for it in items if it["set"] not in {r["set"] for r in results}]

    print(f"\n{'═' * 62}\n批量结果\n{'═' * 62}")
    for r in results:
        print(f"  {'✓' if r['ok'] else '✗'} {r['set']:<28} "
              f"{(r.get('detail') or r.get('error') or '')[:60]}")
    for it in skipped:
        print(f"  · {it['set']:<28} 未跑（批次提前结束）")
    print(f"\n  成功 {len(good)} / 失败 {len(bad)} / 未跑 {len(skipped)}"
          f"   总耗时 {total // 60} 分 {total % 60} 秒"
          + ("  [DRY：没有真的出片]" if a.dry else ""))

    # 落一份报告：批量跑完是「过几天还想查当时到底跑没跑」的事。
    # dry 的报告加 _dry 后缀 —— 预演不该在档案里跟真跑混在一起。
    rep = os.path.join(root, "08_过程",
                       f"batch-{stamp}{'_dry' if a.dry else ''}.json")
    try:
        os.makedirs(os.path.dirname(rep), exist_ok=True)
        with open(rep, "w", encoding="utf-8") as f:
            json.dump({"ip": e["name"], "at": stamp, "dry": a.dry,
                       "tier": a.tier or defaults.get("tier"),
                       "items": items, "results": results}, f,
                      ensure_ascii=False, indent=2)
        print(f"  报告 {rep}")
    except Exception as ex:
        print(f"  ⚠ 报告没写成：{ex}")

    if a.json:
        print(json.dumps({"ok": not bad, "good": len(good), "bad": len(bad),
                          "skipped": len(skipped), "report": rep}, ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
