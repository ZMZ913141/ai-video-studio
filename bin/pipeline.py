#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline —— 一条龙编排器（引擎侧）

它做的事只有一件：按 config/pipeline.json 把 6~7 个脚本串起来跑，
并在每一步之间做「产物在不在」的判断，实现断点续跑。

三条设计边界（别越界）
======================
1. **不发明逻辑**：它不重新实现任何算法，只是按定义好的顺序调脚本。
   参数怎么算在 bin/studio_algo.py，流程怎么排在 config/pipeline.json。
2. **不猜**：产物不在就是没跑过，不会「应该跑过了吧」。
3. **不做重活决策**：跑不跑由人定（--mode step 逐步确认），它只负责不跑错。

用法:
  pipeline.py <IP> <套次>                        # 默认 std 档，一条龙
  pipeline.py 洁斌 S05 --tier fast               # 低分辨率试跑
  pipeline.py 洁斌 S05 --mode step               # 每步确认
  pipeline.py 洁斌 S05 --from video --to burn    # 只跑某一段
  pipeline.py 洁斌 S05 --dry                     # 只打印命令，不执行
  pipeline.py 洁斌 S05 --force                   # 已有产物也重跑

退出码: 0 全通过 / 1 有步骤失败 / 2 配置或前置缺失
"""
import argparse
import glob
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
sys.path.insert(0, BIN_DIR)

import studio_algo as A                      # noqa: E402
import vstudio as V                          # noqa: E402

CFG_PATH = os.path.join(ROOT, "config", "pipeline.json")
FF_FALLBACK = "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"


def load_cfg():
    if not os.path.exists(CFG_PATH):
        sys.exit(f"✗ 缺编排定义：{CFG_PATH}")
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 小工具 ----------------
def newest(pattern):
    """最新的产物文件。★ 0 字节的不算产物。

    为什么：ffmpeg 失败时会**先把输出文件建出来**再报错退出，
    留下一个 0 字节的空壳。下一轮断点续跑看到"文件在"就跳过，
    于是拿着空壳去烧字幕 —— 报错看着像字幕脚本的问题，实际源头是空壳。
    （2026-10-28 23:53 就在这条上卡了一次）
    """
    fs = [p for p in glob.glob(pattern)
          if os.path.isfile(p) and os.path.getsize(p) > 0]
    return max(fs, key=os.path.getmtime) if fs else None


def render(t, ctx, missing=None):
    """把 {key} 换成 ctx 的值。缺的键原样保留并记一笔（不静默吞掉）。"""
    def sub(m):
        k = m.group(1)
        if k in ctx:
            return str(ctx[k])
        if missing is not None:
            missing.add(k)
        return m.group(0)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, t or "")


def ff_dur(path, ff):
    """读音频/视频时长。★ ffprobe 在本机不可用，只能用 ffmpeg -i 解析 stderr。"""
    p = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode(errors="replace"))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def pick_script_txt(d05):
    """在 05_文案/<套次> 里找口播文案：字幕文本 > txt > md。"""
    if not os.path.isdir(d05):
        return None
    for pat in ("*字幕文本*.txt", "*.txt", "*.md"):
        fs = sorted(glob.glob(os.path.join(d05, pat)))
        fs = [f for f in fs if not os.path.basename(f).startswith("job")]
        if fs:
            return fs[0]
    return None


# ---------------- 编排器 ----------------
class Pipeline:
    def __init__(self, ip_entry, setname, cfg, tier="std", mode="auto",
                 dry=False, force=False, segs=None, grid=None, verbose=True,
                 seed=None, reseed=False):
        self.e = ip_entry
        self.set = setname
        self.cfg = cfg
        self.tier = tier
        self.mode = mode
        self.dry = dry
        self.force = force
        self.opt_segs = segs
        self.opt_grid = grid
        self.verbose = verbose
        self.want_seed = seed
        self.reseed = reseed
        self.log = {"ip": ip_entry["name"], "set": setname, "tier": tier,
                    "mode": mode, "dry": dry,
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "steps": [], "plan": None}

        m = re.match(r"^([A-Za-z]+\d+)", setname)
        self.jid = m.group(1) if m else setname
        self.root = ip_entry["path"]
        self.dirs = V.set_dirs(self.root, setname)
        for k, d in self.dirs.items():
            if k in ("06", "07", "08"):
                os.makedirs(d, exist_ok=True)

        self.pys = cfg.get("pythons", {})
        self.ff = cfg.get("ffmpeg") or FF_FALLBACK
        t = cfg.get("tiers", {}).get(tier)
        if t is None:
            sys.exit(f"✗ 没有这个档位：{tier}（可选 {', '.join(cfg.get('tiers', {}))}）")
        self.tier_cfg = t
        d = cfg.get("defaults", {})
        self.ctx = {
            "jid": self.jid,
            "set": setname,
            "ip_root": self.root,
            "dir05": self.dirs["05"], "dir06": self.dirs["06"],
            "dir07": self.dirs["07"], "dir08": self.dirs["08"],
            "scripts": os.path.join(self.root, "03_生产脚本"),
            "tools": os.path.join(self.root, "04_验收工具"),
            "mp": t.get("mp", d.get("mp", 1.26)),
            "pilot": t.get("pilot", ""),
            "dry": "1" if dry else "",
            "overlap": d.get("overlap", 5),
            "maxline": d.get("maxline", 12),
            "crf": d.get("crf", 18),
            "target_seg_secs": d.get("target_seg_secs", 8.0),
        }
        self.script_txt = pick_script_txt(self.dirs["05"])
        if self.script_txt:
            self.ctx["script_txt"] = self.script_txt
        # ★ 种子：以前写死在 gen_video.py 里（881208001），pipeline 不传也不显示。
        #   后果是「重跑一遍」= 同一提示词 + 同一音频 + 同一种子 = 几乎一样的画面，
        #   十几分钟 GPU 白烧，人还以为自己换了一版。现在种子是编排器的一等参数。
        self.seed, self.seed_prev = self.resolve_seed()
        self.ctx["seed"] = str(self.seed)
        self.log["seed"] = self.seed
        self.log["seed_prev"] = self.seed_prev
        # ★ 先把**已存在**的产物填进 ctx，再算自适应参数，再补填一次
        #   （grade/mix/burn 的 out_glob 带 {audio_dur}，要等音频时长出来才认得）。
        #   不这么做的话，用 --from 从中间步骤起跑就会「找不到前置产物」。
        self.prefill()
        self.refresh()
        self.prefill()

    # -------- 种子：记录 + 解析 --------
    def runs_path(self):
        d = self.dirs["08"]
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "runs.jsonl")

    def last_seed(self):
        """上一轮真跑用的种子。没有记录 → None（第一次跑）。"""
        p = self.runs_path()
        if not os.path.exists(p):
            return None
        last = None
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("seed"):
                        last = int(d["seed"])
        except OSError:
            return None
        return last

    def resolve_seed(self):
        """本次用哪个种子。返回 (本次, 上次)。

        --seed N  指定 → 可复现某一次的画
        --reseed  掷一个新的 → 想用同一套提示词再赌一次画面
        都不给    沿用上次的 → 这才是「原样重跑」该有的语义（同一输入同一输出）
        """
        dflt = int(self.cfg.get("defaults", {}).get("seed", 881208001))
        prev = self.last_seed()
        if self.want_seed:
            return int(self.want_seed), prev
        if self.reseed:
            return random.randint(1, 2147483647), prev
        return (prev or dflt), prev

    @property
    def seed_changed(self):
        """画面要不要重出。

        ★ 人显式给了 --seed / --reseed 就算「变了」，哪怕上次没记录过种子。
          否则第一次用 --reseed 时 prev 是 None，判断成「没变」→ video 被跳过，
          人以为换了种子，拿到的还是原来那条片（实测踩到）。
        """
        if self.want_seed or self.reseed:
            return True
        return self.seed_prev is not None and int(self.seed) != int(self.seed_prev)

    def record_run(self, start, end, ok):
        """每次真跑记一行。界面上「这套片用过哪几个种子、哪次出的最好」全靠它。"""
        if self.dry:
            return
        rec = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "seed": int(self.seed), "tier": self.tier,
               "from": start, "to": end, "ok": bool(ok),
               "steps": [{"id": r["id"], "status": r.get("status"),
                          "out": os.path.basename(r.get("out") or "")}
                         for r in self.log.get("steps", [])]}
        try:
            with open(self.runs_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as ex:
            print(f"  ⚠ 跑批记录写不进去：{ex}")

    def prefill(self):
        """扫描各步骤产物，把还活着的填进 ctx 变量（断点续跑的前提）。"""
        for st in self.cfg.get("steps", []):
            var, og = st.get("set_var"), st.get("out_glob")
            if not var or not og:
                continue
            hit = newest(render(og, self.ctx))
            if hit:
                self.ctx[var] = hit

    # -------- 自适应参数：有音频就按真实时长算，没有就按文案字数估 --------
    def refresh(self):
        c = self.ctx
        mp = c["mp"]
        w, h = A.calc_res(mp)
        c["res_w"], c["res_h"] = w, h
        c["maxlen"] = A.maxlen_for_mp(mp)
        c["fast"] = "1" if A.is_fast(mp) else ""

        dur = None
        if c.get("audio") and os.path.isfile(c["audio"]):
            dur = ff_dur(c["audio"], self.ff)
        if dur is None and self.script_txt and os.path.isfile(self.script_txt):
            # 估：ip.json 记的是约 3.85 字/s（时长只由字数决定）
            try:
                n = len(re.sub(r"\s", "", open(self.script_txt, encoding="utf-8-sig").read()))
                dur = n / 3.85
            except Exception:
                dur = None
        if dur is None:
            dur = 30.0
        c["audio_dur_sec"] = dur
        c["audio_dur"] = f"{dur:.1f}"

        p = A.plan(dur, mp, target_segs=self.opt_segs,
                   target_seg_secs=float(c["target_seg_secs"]),
                   overlap=int(c["overlap"]))
        c["segs"] = p["segs"]
        c["grid"] = ",".join(str(x) for x in p["grid"])
        c["plan_json"] = json.dumps(p, ensure_ascii=False)
        c["pad_head"], c["pad_tail"] = p["pad_head"], p["pad_tail"]
        c["bgm_offset"] = str(p["bgm_offset"])
        if self.opt_grid:
            c["grid"] = self.opt_grid
        self.log["plan"] = p
        return p

    # -------- 单步 --------
    def step_cmd(self, st):
        """渲染一步的命令（不执行）。返回 (cmd, env, out_glob)。"""
        miss = set()
        py = self.pys.get(st.get("py", "std"), sys.executable)
        cmd = [py]
        if st.get("builtin"):
            # 内置步骤没有脚本文件，命令栏只写清楚它要干什么
            return [f"[builtin:{st['builtin']}]"], {}, \
                render(st.get("out_glob", ""), self.ctx, miss) or None, miss
        if st.get("script"):
            cmd.append(render(st["script"], self.ctx, miss))
        cmd += [render(a, self.ctx, miss) for a in st.get("argv", [])]
        cmd += [render(a, self.ctx, miss) for a in st.get("flag", [])]
        env = {k: render(v, self.ctx, miss) for k, v in (st.get("env") or {}).items()}
        env = {k: v for k, v in env.items() if v != ""}
        out = render(st.get("out_glob", ""), self.ctx, miss) or None
        return cmd, env, out, miss

    def run_step(self, st):
        sid = st["id"]
        rec = {"id": sid, "stage": st.get("stage"), "desc": st.get("desc", ""),
               "t0": time.time()}
        cmd, env, out, miss = self.step_cmd(st)
        rec["cmd"] = " ".join(cmd)
        rec["env"] = env or None

        if miss:
            # 变量没解析通常是因为**上一步还没跑**（产物路径要等它出来）。
            # DRY 时这是正常的，不算失败；真跑时才是真的缺前置。
            if self.dry:
                rec["status"] = "dry"
                rec["note"] = f"待前序步骤产出：{sorted(miss)}"
                print(f"  · {sid} 缺 {sorted(miss)}（DRY 下正常，真跑时由前序步骤填）")
                self.log["steps"].append(rec)
                return rec
            rec["status"] = "failed"
            rec["error"] = f"模板变量未解析：{sorted(miss)}（先用 --from 跑前面的步骤）"
            self.log["steps"].append(rec)
            return rec

        # 断点续跑：产物在且没有 --force → 跳过
        # ★ 但「产物在」不等于「产物还有效」：上游变了它就必须重做。
        #   最典型的就是留白：加了 0.27s 前导静音后，旧 SRT 整体偏 0.27s，
        #   看着还在、其实已经对不上嘴了 —— 这类错不会报错，只会交付出错位的片。
        if out and not self.force and not self.dry:
            hit = newest(out)
            stale = None
            if hit:
                for dep in st.get("depends", []):
                    src = self.ctx.get(dep)
                    if src and os.path.isfile(src) and \
                            os.path.getmtime(src) > os.path.getmtime(hit) + 1:
                        stale = (dep, os.path.basename(src))
                        break
            # ★ 源文件也是上游：文案 txt 改了，TTS 必须重配，不然整条链会用着
            #   旧音频一路「跳过」到底 —— 日志全是 ⏭，人以为跑过了，交出去的还是旧片。
            #   depends 只认「上一步的产物」，认不出「人改过的源文件」，所以要单独声明。
            if hit and not stale:
                for pat in st.get("inputs", []):
                    m2 = set()
                    raw = render(pat, self.ctx, m2) if "{" in str(pat) else str(pat)
                    if m2 or not raw:
                        continue
                    for f in glob.glob(raw):
                        if os.path.isfile(f) and \
                                os.path.getmtime(f) > os.path.getmtime(hit) + 1:
                            stale = ("input", os.path.basename(f))
                            break
                    if stale:
                        break
            # ★ 换了种子，画面必然要重出。不把这条接上的话，人点「换种子跑」
            #   会看到 video 被跳过（产物在），只有后面几步重做 —— 白跑还骗人。
            if hit and not stale and sid == "video" and self.seed_changed:
                stale = ("seed", f"{self.seed_prev} → {self.seed}")
            if hit and not stale:
                rec["status"] = "skipped"
                rec["out"] = hit
                if st.get("set_var"):
                    self.ctx[st["set_var"]] = hit
                self.log["steps"].append(rec)
                print(f"  ⏭  {sid} 跳过（已有 {os.path.basename(hit)}）")
                self.refresh()
                return rec
            if stale:
                print(f"  ↻ {sid} 重做（上游 {stale[0]} 已更新：{stale[1]}）")

        print(f"\n── {sid} · {st.get('desc','')}")
        if self.verbose:
            env_s = " ".join(f"{k}={v}" for k, v in env.items())
            print(f"   $ {env_s} {' '.join(cmd)}")

        if self.mode == "step" and not self.dry:
            if not sys.stdin.isatty():
                print("   ⚠ 非交互环境，step 模式自动放行（要看每步确认请在终端跑）")
            else:
                ans = input("   回车执行 / s 跳过 / q 退出：").strip().lower()
                if ans == "q":
                    rec["status"] = "quit"
                    self.log["steps"].append(rec)
                    return rec
                if ans == "s":
                    rec["status"] = "skipped"
                    self.log["steps"].append(rec)
                    return rec

        if self.dry:
            rec["status"] = "dry"
            self.log["steps"].append(rec)
            return rec

        if st.get("builtin") == "audio_pad":
            rc = self._builtin_pad(rec)
        else:
            # STUDIO_HOME：让 IP 目录里的脚本副本也能找到仓库（比如
            # config/secrets.local.json 里的第三方 key）。不传的话，
            # 从 /cfu/ai视频档案库/<IP>/03_生产脚本 往上走是找不到仓库的。
            rc = subprocess.run(cmd, cwd=self.root,
                                env=dict(os.environ, STUDIO_HOME=ROOT, **env)).returncode
        rec["rc"] = rc
        rec["t1"] = time.time()
        rec["sec"] = round(rec["t1"] - rec["t0"], 1)

        if rc != 0:
            rec["status"] = "failed" if not st.get("optional") else "warn"
            rec["error"] = f"退出码 {rc}"
            # ★ 把这一步**刚生成的空壳产物**清掉。否则下一轮 --from 会把它
            #   当成"已有产物"跳过，然后拿着 0 字节文件往下跑。
            #   只删本步时间窗内新建的、且属于本步 out_glob 的文件，不碰旧文件。
            if out:
                for f in glob.glob(out):
                    try:
                        if os.path.isfile(f) and os.path.getmtime(f) >= rec["t0"] - 1:
                            sz = os.path.getsize(f)
                            os.remove(f)
                            print(f"    清掉本步产生的半成品：{os.path.basename(f)}"
                                  f"（{sz} 字节）")
                    except OSError:
                        pass
            if st.get("optional"):
                print(f"  ⚠ {sid} 报问题（rc={rc}）—— 这是**验收**步骤，"
                      f"不影响成片，但结论要看一眼")
            else:
                print(f"  ✗ {sid} 失败（rc={rc}）—— 流水线停在这里，"
                      f"修好后用 --from {sid} 接着跑")
        else:
            hit = newest(out) if out else None
            # 有些脚本把产物写在自己目录里（gen_video.py 写在 03_生产脚本），
            # 编排器负责归位到 07_成片/<套次>/ —— 不然断点续跑永远看不到它。
            if st.get("move_from"):
                src = newest(render(st["move_from"], self.ctx))
                if src:
                    dst = os.path.join(render(st["move_to"], self.ctx),
                                       os.path.basename(src))
                    os.replace(src, dst)
                    hit = dst
            if out and not hit:
                rec["status"] = "failed"
                rec["error"] = f"跑完了但没找到产物：{out}"
                print(f"  ✗ {sid} 无产物：{out}")
            else:
                rec["status"] = "ok"
                rec["out"] = hit
                if st.get("set_var") and hit:
                    self.ctx[st["set_var"]] = hit
                print(f"  ✓ {sid} 完成"
                      + (f" → {os.path.basename(hit)}" if hit else "")
                      + (f"  ({rec['sec']}s)" if rec.get("sec") else ""))
                # 重做之后旧版本还躺在同一目录（TTS 产物名带时长：22.2s → 21.8s
                # 就会多出一份，交付时看到两个版本不知道用哪个）。
                # 只清本步 glob 命中的、早于本次开跑的；别的步骤一概不碰 ——
                # 07_成片 里同一个 jid 还有 raw/成片/混音/字幕版好几种形态。
                if out and hit and not self.dry:
                    for f in glob.glob(out):
                        try:
                            if os.path.isfile(f) and os.path.abspath(f) != os.path.abspath(hit) \
                                    and os.path.getmtime(f) < rec["t0"] - 1:
                                os.remove(f)
                                print(f"    清掉本步的旧版本：{os.path.basename(f)}")
                        except OSError:
                            pass
        self.log["steps"].append(rec)
        self.refresh()
        return rec

    # -------- 内置步骤：音频前后留白 --------
    def _builtin_pad(self, rec):
        src = self.ctx.get("audio_tts")
        if not src or not os.path.isfile(src):
            print("  ✗ pad 找不到 TTS 音频（先跑 tts 步）")
            return 1
        d = ff_dur(src, self.ff)
        h, t = A.audio_pads(d or 30.0)
        dst = os.path.join(self.dirs["06"], f"{self.jid}_音频_留白_{(d or 0)+h+t:.1f}s.mp3")
        ms = int(round(h * 1000))
        total = (d or 0) + h + t
        # adelay 把整段往后推 head，apad 在尾部补 tail；-t 截断（apad 不截会无限长）
        cmd = [self.ff, "-y", "-i", src,
               "-af", f"adelay={ms}:all=1,apad=pad_dur={t}",
               "-t", f"{total:.3f}", "-ar", "48000", "-c:a", "libmp3lame",
               "-q:a", "2", dst]
        print(f"   留白 前 {h}s / 后 {t}s（自适应 {d:.1f}s 片长）")
        rc = subprocess.run(cmd, capture_output=True).returncode
        if rc == 0 and os.path.isfile(dst):
            self.ctx["audio"] = dst
            rec["out"] = dst
        return rc

    # -------- 跑全流程 --------
    def seed_from_disk(self):
        """按步骤顺序，把**已经存在**的产物路径填回 ctx。

        为什么需要：`--from mix` 这种从中间接手时，前面的步骤根本没被执行，
        没人给 ctx["graded"] 赋值 → mix 报「模板变量未解析：['graded']」，
        可文件明明就躺在 07_成片 里。这不是缺前置，是断点续跑只认"跑过的"、
        不认"已经在的"。文件系统才是真相来源，所以这里按 out_glob 回填。
        """
        for st in self.cfg.get("steps", []):
            var = st.get("set_var")
            if not var or var in self.ctx or not st.get("out_glob"):
                continue
            miss = set()
            out = render(st["out_glob"], self.ctx, miss)
            if out and not miss:
                hit = newest(out)
                if hit:
                    self.ctx[var] = hit

    def run(self, start=None, end=None):
        steps = self.cfg.get("steps", [])
        ids = [s["id"] for s in steps]
        if start and start not in ids:
            sys.exit(f"✗ --from {start} 不在步骤里（可选 {'/'.join(ids)}）")
        if end and end not in ids:
            sys.exit(f"✗ --to {end} 不在步骤里（可选 {'/'.join(ids)}）")
        i0 = ids.index(start) if start else 0
        i1 = ids.index(end) + 1 if end else len(steps)
        self.seed_from_disk()

        p = self.log["plan"]
        print(f"{self.e['name']} · {self.set}   档位 {self.tier} "
              f"({self.ctx['res_w']}x{self.ctx['res_h']}, MP={self.ctx['mp']})"
              + ("  [DRY]" if self.dry else ""))
        print(f"  预计 {p['total_frames']} 帧 / {p['total_sec']}s / {p['segs']} 段"
              f"  单段上限 {p['maxlen']} 帧  格数 {p['grid']}")
        if self.ctx.get("pad_head"):
            print(f"  音频留白 前 {self.ctx['pad_head']}s / 后 {self.ctx['pad_tail']}s"
                  f"  配乐比人声低 {self.ctx['bgm_offset']} LU")
        # 种子必须显示出来：不显示的话，人不知道自己这版和上版是不是同一种子，
        # 「怎么重跑都是同一张脸」就会变成玄学。
        if self.reseed:
            sd_tag = "（新掷的）"
        elif self.want_seed:
            sd_tag = f"（指定；上次 {self.seed_prev}）" if self.seed_prev else "（指定）"
        elif self.seed_prev:
            sd_tag = f"（沿用上次 {self.seed_prev}）"
        else:
            sd_tag = "（配置默认，这套片还没跑过）"
        print(f"  种子 {self.seed}{sd_tag}"
              + ("  ← 换过了，video 及下游会重出" if self.seed_changed
                 else "  ← 没换，画面会跟上次几乎一样" if self.seed_prev else ""))
        if not self.script_txt:
            print("  ⚠ 没找到文案 txt —— tts/srt 两步会失败")

        for st in steps[i0:i1]:
            rec = self.run_step(st)
            if rec["status"] in ("failed", "quit"):
                break

        self.save_log()
        if not self.dry:
            try:
                self.sync_job()
            except Exception as ex:      # 同步失败不该让出片结果变成失败
                print(f"  ⚠ job.json 同步失败：{ex}")
            self.record_run(start, end,
                            not [r for r in self.log["steps"]
                                 if r.get("status") == "failed"])
        return self.summary()

    def sync_job(self):
        """把跑完的步骤回写 job.json 的阶段表 —— 看板（CLI/UI）才有得看。

        ★ 不顺手做这件事的话，流水线跑完了 `vstudio jobs` 还显示「缺音频缺字幕」，
          因为 job.json 的阶段是**人工标记**，它不知道流水线干了什么。
        """
        p = V.job_path(self.root, self.set)
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            job = json.load(f)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for r in self.log["steps"]:
            if r.get("status") in ("ok", "skipped") and r.get("stage"):
                st = job.setdefault("stages", {}).setdefault(r["stage"], {})
                st["status"] = "ok"
                st["at"] = now
                st["by"] = "pipeline:" + r["id"]
        job["artifacts"] = V.scan_artifacts(self.root, self.set)
        job["stage"] = V.derive_stage(job)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)

    def save_log(self):
        d = self.dirs["08"]
        os.makedirs(d, exist_ok=True)
        name = f"pipeline-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.log, f, ensure_ascii=False, indent=2)
        self.log_path = path
        return path

    def summary(self):
        rows = self.log["steps"]
        bad = [r for r in rows if r["status"] == "failed"]
        print("\n" + "─" * 60)
        for r in rows:
            mark = {"ok": "✓", "skipped": "⏭", "dry": "·", "warn": "⚠",
                    "failed": "✗", "quit": "■"}.get(r["status"], "?")
            tail = r.get("out") and os.path.basename(r["out"]) or r.get("error", "")
            print(f"  {mark} {r['id']:<6} {str(r.get('stage') or ''):<4} {tail}")
        print(f"  日志 {os.path.basename(getattr(self, 'log_path', ''))}")
        return {"ok": not bad, "steps": rows, "log": getattr(self, "log_path", None)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="一条龙出片编排器")
    ap.add_argument("ip", nargs="?", default="洁斌")
    ap.add_argument("set")
    ap.add_argument("--mode", default="auto", choices=["auto", "step"])
    ap.add_argument("--tier", default=None, help="fast/pilot/std/high")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="已有产物也重跑（不看输入有没有变，一律重做）")
    ap.add_argument("--seed", type=int, default=None,
                    help="指定种子：同一个种子能复现同一次的画面")
    ap.add_argument("--reseed", action="store_true",
                    help="换一个新种子重出画面（同一套提示词再赌一次）")
    ap.add_argument("--segs", type=int, help="强制段数（不给则按时长自适应）")
    ap.add_argument("--grid", help="手动给 17 格数，如 9,10,7,7,11,9")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_cfg()
    tier = a.tier or cfg.get("defaults", {}).get("tier", "std")
    e = V.resolve_ip(a.ip)
    sets = V.list_sets(e["path"])
    if a.set not in sets:
        # 允许只给编号（S05）
        hit = [s for s in sets if s.startswith(a.set)]
        if not hit:
            sys.exit(f"✗ {e['name']} 没有这套：{a.set}（现有 {' / '.join(sets) or '无'}）")
        a.set = hit[0]

    p = Pipeline(e, a.set, cfg, tier=tier, mode=a.mode, dry=a.dry,
                 force=a.force, segs=a.segs, grid=a.grid,
                 seed=a.seed, reseed=a.reseed)
    r = p.run(start=a.start, end=a.end)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
