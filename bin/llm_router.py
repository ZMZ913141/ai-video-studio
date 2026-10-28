#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多供应商 LLM 路由：限额自动切下一家，永不因为某家用完而停。

为什么需要它
============
免费额度平台有三类失败，处理方式必须不同：
  1. **配额/限流**（429、配额不足、余额不足）→ 冷却一段时间后**还会恢复**，
     每天重置的平台应当冷却到次日 0 点。
  2. **鉴权失败**（401/403）→ key 填错或没填，冷却没意义，直接标记停用并提示改配置。
  3. **服务端故障/超时**（5xx、网络错误）→ 短暂冷却后重试。

本路由按 priority 依次尝试；全部不可用时**不会抛异常停摆**，
而是解除「最早到期」的那家再试一轮并告警 —— 宁可用一个刚过限流的，
也不要让流水线停住。

用法
====
    set -a && . config/llm.env && set +a
    python3 bin/llm_router.py status                  # 看各家用没用、冷却到几点
    python3 bin/llm_router.py chat "写一句口播开场白"    # 实测走哪家
    python3 bin/llm_router.py reset                   # 手动清空所有冷却

作为库用：
    from llm_router import LLMRouter
    r = LLMRouter()
    d = r.chat([{"role":"user","content":"..."}], tools=TOOLS)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = ROOT / "config" / "providers.json"
STATE = ROOT / "config" / ".llm_state.json"
# 人手动选的首选车道。**只是「优先」，不是「只用」** ——
# 首选挂了照样自动换下一家，否则选一条不稳的通道就等于把整条流水线焊死。
PREFS = ROOT / "config" / ".llm_prefs.json"

# 判定为「配额/限流」的关键词（中英文平台混用，命中即冷却后换下一家）
QUOTA_HINTS = ["quota", "配额", "额度", "余额", "insufficient", "balance",
               "rate limit", "rate_limit", "ratelimit", "too many requests",
               "exceeded", "超出", "limit reached", "payment required",
               "免费额度", "已用尽", "insufficient_quota"]
# 判定为「鉴权失败」——冷却没意义，直接停用
AUTH_HINTS = ["unauthorized", "invalid api key", "invalid_api_key", "authentication",
              "forbidden", "no auth", "鉴权", "未认证", "api key 无效", "invalid token"]
# 判定为「配置未完成」——模型没开通/没权限，改的是控制台不是代码。
# 冷却到次日 0 点（每天自动重试一次），别 5 分钟一次空转。
# 典型：火山方舟 ModelNotOpen「has not activated the model ... in the Ark Console」
CONFIG_HINTS = ["modelnotopen", "not activated", "has not activated",
                "invalidendpointormodel", "does not exist or you do not have access",
                "未开通", "未激活", "无权限", "invalid_model"]
# 判定为「瞬时拥塞」——和额度耗尽不同：它几十秒到几分钟就恢复，
# 所以只按 rate_limit_cooldown_min（默认 5 分钟，拥塞型平台可设 1 分钟）短冷却。
# 典型：智谱 1305「该模型当前访问量过大」
RATE_HINTS = ["访问量过大", "1305", "系统繁忙", "busy", "overloaded", "over rate limit",
              "concurrent", "concurrency", "服务器繁忙", "稍后再试", "try again later"]


def _now() -> float:
    return time.time()


def _next_local_midnight() -> float:
    n = datetime.now()
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp()


def _fmt(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class LLMRouter:
    def __init__(self, config_path: Path = CFG, state_path: Path = STATE):
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.defaults = self.cfg.get("defaults", {})
        self.state_path = Path(state_path)
        self.providers = sorted(
            [p for p in self.cfg["providers"] if p.get("enabled", False)],
            key=lambda p: p.get("priority", 999))
        self.state = self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        try:
            os.chmod(self.state_path, 0o600)
        except Exception:
            pass

    def _cool(self, name: str, until: float, reason: str):
        self.state[name] = {"until": until, "reason": reason, "at": _now()}
        self._save()

    def _available(self, p: dict) -> bool:
        """冷却已过就算恢复（顺手清掉过期记录）。"""
        st = self.state.get(p["name"])
        if not st:
            return True
        if st.get("until", 0) <= _now():
            self.state.pop(p["name"], None)
            self._save()
            return True
        return False

    def _cooldown_until(self, p: dict, kind: str) -> float:
        # 模型没开通：要人去控制台点，代码改不了 → 冷却到次日 0 点，每天重试一次
        if kind == "config":
            return _next_local_midnight()
        if kind == "quota":
            if p.get("daily_reset", self.defaults.get("daily_reset", False)):
                return _next_local_midnight()
            return _now() + 60 * float(p.get("quota_cooldown_min",
                                             self.defaults.get("quota_cooldown_min", 120)))
        return _now() + 60 * float(p.get("rate_limit_cooldown_min",
                                         self.defaults.get("rate_limit_cooldown_min", 5)))

    # ------------------------------------------------- 能力 / 涉密 过滤
    # need="vision" 要的是「能看图」这条能力，不是某个模型名。
    # 以前是硬编码记「智谱不能看图、硅基 Qwen3-VL 能看图」，人一走这个约定就丢，
    # 而且新加一家还要回头改代码。现在能力写在 providers.json 里，代码只读它。
    NEED_CAPABILITY = {"vision": "image", "image": "image", "text": "text"}

    def _pool(self, sensitive: bool = True, need: str | None = None) -> list[dict]:
        """按「能力」+「能不能碰涉密内容」挑通道。

        sensitive=True（默认）→ 排除所有 sensitive=false 的第三方免费池。
        ⚠ 这里**不做静默降级**：宁可报错让人明说，也不能为了跑通
        就把客户台词悄悄送进「提示词可能被上游记录」的免费池。
        """
        ps = self.providers
        if need:
            want = self.NEED_CAPABILITY.get(need, need)
            ps = [p for p in ps if want in (p.get("capabilities") or ["text"])]
        if sensitive:
            ps = [p for p in ps if p.get("sensitive", True)]
        return ps

    def list_models(self, provider: str) -> list[dict]:
        """查某条通道实际有哪些模型 + 能力元数据（网关类通道带 input_modalities）。

        这是「provider 自己声明的能力」的取数口，用来核对 providers.json
        里手写的能力对不对 —— 网关/聚合型通道的模型会变，channels 一改就可能对不上。
        """
        ps = [p for p in self.cfg["providers"] if p["name"] == provider]
        if not ps:
            raise RuntimeError(f"没有叫 {provider} 的通道")
        p = ps[0]
        key = os.environ.get(p.get("key_env", ""), "")
        req = urllib.request.Request(
            p["base_url"].rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {key}"} if key else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("data", [])

    # ---------------------------------------------------------------- 请求
    @staticmethod
    def _classify(code: int, body: str) -> str:
        low = (body or "").lower()
        if code in (401, 403) or any(h in low for h in AUTH_HINTS):
            return "auth"
        # 先读 body 再读状态码：智谱「访问量过大」用的就是 429，但它是瞬时拥塞，
        # 若按 quota 处理会被冻一小时。报文内容比状态码更能说明到底发生了什么。
        if any(h in low for h in RATE_HINTS):
            return "rate"
        if any(h in low for h in CONFIG_HINTS):
            return "config"
        if code == 429 or code == 402 or any(h in low for h in QUOTA_HINTS):
            return "quota"
        if code >= 500:
            return "server"
        return "other"

    def _call(self, p: dict, body: dict) -> dict:
        key = os.environ.get(p.get("key_env", ""), "")
        if not key:
            raise RuntimeError(f"MISSING_KEY: 环境变量 {p.get('key_env')} 未设置")
        req = urllib.request.Request(
            p["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        timeout = float(p.get("timeout_sec", self.defaults.get("timeout_sec", 90)))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def chat(self, messages: list, tools: list | None = None, tool_choice=None,
             response_format: dict | None = None, max_tokens: int = 1024,
             temperature: float = 0.2, verbose: bool = True,
             sensitive: bool = True, need: str | None = None,
             prefer: str | None = None) -> dict:
        """依次尝试所有可用供应商；全部失败才抛异常。

        sensitive  默认 True =「这段内容属于客户/商业信息」→ 自动跳过第三方免费池
                   （providers.json 里 sensitive=false 的那些）。
                   只有视觉验收、通用草稿这类不含客户信息的场合才显式传 False。
        need       'vision' 等能力名 → 只在支持该能力的通道里挑（见 _pool）。
        prefer     首选车道名（界面里选的）。该车道在候选池里就排到最前，
                   不在（被能力/涉密过滤掉、或没启用）就**忽略它继续按 priority 走**。
                   刻意不实现「只走这一条」—— 首选不稳时那等于让流水线停摆。
        """
        body_base = {"messages": messages, "max_tokens": max_tokens,
                     "temperature": temperature}
        if tools:
            body_base["tools"] = tools
        if tool_choice:
            body_base["tool_choice"] = tool_choice
        if response_format:
            body_base["response_format"] = response_format

        pool = self._pool(sensitive=sensitive, need=need)
        if not pool:
            raise RuntimeError(
                f"没有可用通道（need={need}, sensitive={sensitive}）。\n"
                + (f"  需要 {need} 能力，但没有一条既支持它、又允许处理涉密内容的通道。\n"
                   f"  若这些内容不含客户信息，显式传 sensitive=False 才会用到免费视觉池。\n"
                   if need else "")
                + f"  当前 enabled 通道：{[p['name'] for p in self.providers]}")
        pool = sorted(pool, key=lambda p: p.get("priority", 999))

        # 首选车道排到最前（只在它本来就在候选池里时生效）
        want = prefer or self.prefs().get("prefer")
        if want:
            hit = [p for p in pool if p["name"] == want]
            if hit:
                pool = hit + [p for p in pool if p["name"] != want]
            elif verbose:
                print(f"[router] ⚠ 首选车道 {want} 不在本次候选池"
                      f"（未启用 / 不支持该能力 / 被涉密规则排除），按 priority 走",
                      file=sys.stderr)

        errors = []
        for attempt in range(2):          # 第二轮：强制解除最早到期的冷却再试
            for p in pool:
                if attempt == 0 and not self._available(p):
                    continue
                body = dict(body_base, model=p["model"])
                # 每家可自带 extra_body（例：智谱 "thinking": {"type":"disabled"}）
                if p.get("extra_body"):
                    body.update(p["extra_body"])
                t0 = _now()
                try:
                    d = self._call(p, body)
                    d = self._normalize(d)
                    self.state.pop(p["name"], None)
                    self.state[p["name"] + "_last_ok"] = {"at": t0}
                    self._save()
                    if verbose:
                        print(f"[router] ✓ {p['name']} / {p['model']}  {_now()-t0:.1f}s",
                              file=sys.stderr)
                    d["_provider"] = p["name"]
                    return d
                except urllib.error.HTTPError as e:
                    body_txt = ""
                    try:
                        body_txt = e.read().decode()[:300]
                    except Exception:
                        pass
                    kind = self._classify(e.code, body_txt)
                    msg = f"{p['name']}: HTTP {e.code} ({kind}) {body_txt[:100]}"
                except Exception as e:
                    kind = "server" if isinstance(e, TimeoutError) else "other"
                    msg = f"{p['name']}: {type(e).__name__} {str(e)[:120]}"

                errors.append(msg)
                if kind == "auth":
                    self._cool(p["name"], _next_local_midnight() + 86400 * 365,  # 近乎永久
                               f"auth 失败，检查 {p.get('key_env')}")
                    if verbose:
                        print(f"[router] ✗ {msg} → 停用（key 有问题，改好配置后跑 reset）",
                              file=sys.stderr)
                else:
                    reason = ("模型未开通/无权限 → 去控制台开通后跑 reset"
                              if kind == "config" else kind)
                    self._cool(p["name"], self._cooldown_until(p, kind), reason)
                    if verbose:
                        print(f"[router] ✗ {msg} → 冷却至 {_fmt(self.state[p['name']]['until'])}，"
                              f"换下一家", file=sys.stderr)

            if attempt == 0:
                # 全军覆没：解除最早到期的那家，再来一轮（绝不轻易停摆）
                cands = [(v["until"], k) for k, v in self.state.items()
                         if isinstance(v, dict) and v.get("until")]
                if not cands:
                    break
                until, name = min(cands)
                print(f"[router] ⚠ 所有供应商都在冷却。强制提前启用最早恢复的 {name}"
                      f"（原定 {_fmt(until)}）", file=sys.stderr)
                self.state.pop(name, None)
                self._save()

        raise RuntimeError("所有供应商均不可用：\n  " + "\n  ".join(errors))

    def _normalize(self, d: dict) -> dict:
        """把各家返回里的怪格式统一成 OpenAI 形状。

        ⚠ 网关和部分 Anthropic 兼容层会把 content 返回成
        `[{"type":"text","text":"..."}]` 而不是字符串。上层（prompts.py 等）
        直接拿 content 当字符串拼，拿到 list 会崩在半路，
        报错还跟「答案错了」长得一模一样，很难查。统一在这里抹平。
        """
        try:
            msg = d["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return d
        c = msg.get("content")
        if isinstance(c, list):
            msg["content"] = "".join(
                part.get("text", "") for part in c
                if isinstance(part, dict) and part.get("type") == "text")
        return d

    # ---------------------------------------------------------------- 运维
    def prefs(self) -> dict:
        try:
            return json.loads(PREFS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_prefer(self, name: str | None):
        """记住人手动选的首选车道。传 None / 空串 = 清掉，回到纯 priority 排序。"""
        d = self.prefs()
        if name:
            d["prefer"] = name
        else:
            d.pop("prefer", None)
        d["at"] = _fmt(_now())
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return d

    def status(self) -> list[dict]:
        rows = []
        for p in self.cfg["providers"]:
            key = os.environ.get(p.get("key_env", ""), "")
            st = self.state.get(p["name"], {})
            cooling = bool(st.get("until")) and st["until"] > _now()
            caps = p.get("capabilities") or ["text"]
            rows.append({
                "name": p["name"], "enabled": p.get("enabled", False),
                "priority": p.get("priority"), "model": p.get("model"),
                "key": ("已配置" if key else "缺 key"),
                "state": ("冷却至 " + _fmt(st["until"])) if cooling else "可用",
                "reason": st.get("reason", ""),
                "vision": "✓" if "image" in caps else "",
                "pool": "免费池" if p.get("sensitive", True) is False else "付费",
                "caps": caps,
                "prefer": p["name"] == self.prefs().get("prefer"),
            })
        return rows

    def reset(self):
        self.state = {}
        self._save()


def main():
    ap = argparse.ArgumentParser(description="多供应商 LLM 路由")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "chat", "reset", "models", "vision",
                             "prefer", "probe"])
    ap.add_argument("text", nargs="?", default="回复四个字：路由正常")
    ap.add_argument("--image", help="看图任务的图片路径（need=vision）")
    ap.add_argument("--provider", help="models / probe 命令限定某条通道")
    a = ap.parse_args()

    r = LLMRouter()
    if a.cmd == "status":
        rows = [x for x in r.status() if x["enabled"]]
        if not rows:
            print("没有 enabled=true 的供应商 —— 去 config/providers.json 打开")
        for x in rows:
            # 冷却/停用的原因必须显示出来，否则「可用/冷却」两态会误导人；
            # 但已恢复的家不该继续挂着上次的原因
            tail = f"  ← {x['reason']}" if (x['reason'] and x['state'] != "可用") else ""
            star = "★" if x.get("prefer") else " "
            print(f" {star}{x['priority']:>4}  {x['name']:<12} {x['key']:<7} "
                  f"{x['state']:<18} {x['vision'] or ' '} {x['pool']:<4} "
                  f"{x['model']}{tail}")
        pre = r.prefs().get("prefer")
        print(f"\n  ★ = 首选车道（{pre or '未设，按 priority'}）。"
              f"首选只是优先，挂了会自动换下一家。")
        dis = [x["name"] for x in r.status() if not x["enabled"]]
        print(f"\n  未启用（注册后打开）: {', '.join(dis)}")
        print("  视觉 = 支不支持看图；付费/免费池 = 客户内容只走「付费」那一栏")
    elif a.cmd == "reset":
        r.reset()
        print("✓ 已清空所有冷却状态")
    elif a.cmd == "prefer":
        # 传空 = 清掉首选，回到纯 priority
        d = r.set_prefer(a.provider)
        print(f"✓ 首选车道 = {d.get('prefer') or '（无，按 priority 排序）'}")
        print("  注意它只是「优先」不是「只用」：这条挂了照样自动换下一家。")
    elif a.cmd == "probe":
        # 只测一条指定通道，**不写冷却** —— 诊断用，不该影响真实路由状态。
        name = a.provider or (r.prefs().get("prefer") or "")
        if not name:
            sys.exit("probe 需要 --provider <车道名>")
        p = next((x for x in r.cfg["providers"] if x["name"] == name), None)
        if not p:
            sys.exit(f"没有叫 {name} 的通道")
        t0 = _now()
        try:
            body = {"messages": [{"role": "user", "content": a.text}],
                    "max_tokens": 32, "temperature": 0, "model": p["model"]}
            if p.get("extra_body"):
                body.update(p["extra_body"])
            d = r._normalize(r._call(p, body))
            ans = (d["choices"][0]["message"].get("content") or "").strip()[:60]
            print(f"✓ {name} 通  {_now()-t0:.1f}s  「{ans}」")
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode()[:160]
            except Exception:
                txt = ""
            print(f"✗ {name}  HTTP {e.code} ({r._classify(e.code, txt)}) {txt}")
            sys.exit(1)
        except Exception as e:
            print(f"✗ {name}  {type(e).__name__} {str(e)[:160]}")
            sys.exit(1)
    elif a.cmd == "models":
        # 核对手写能力与上游实际声明是否一致（网关类通道模型会变，得能查）
        names = [a.provider] if a.provider else [
            p["name"] for p in r.cfg["providers"] if p.get("enabled")]
        for n in names:
            try:
                ms = r.list_models(n)
            except Exception as e:
                print(f"  {n:<14} 查不到：{type(e).__name__} {e}")
                continue
            vis = [m["id"] for m in ms
                   if "image" in (m.get("input_modalities") or [])
                   or m.get("supportsImages")]
            print(f"  {n:<14} {len(ms):>3} 个模型，其中 {len(vis)} 个声明支持看图")
            for m in vis[:6]:
                print(f"      · {m['id']}")
    elif a.cmd == "vision":
        if not a.image:
            sys.exit("vision 需要 --image 图片路径")
        import base64, mimetypes
        mime = mimetypes.guess_type(a.image)[0] or "image/png"
        b64 = base64.b64encode(open(a.image, "rb").read()).decode()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": a.text},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]
        # ⚠ 视觉能力目前只有免费池提供，所以这条命令必须 sensitive=False。
        #   看图内容若涉及客户素材/未发布成片，别用这里，改走付费 VL（硅基 Qwen3-VL）。
        d = r.chat(msgs, need="vision", sensitive=False, max_tokens=600)
        print(f"通道: {d['_provider']}")
        print(d["choices"][0]["message"].get("content", "")[:800])
    else:
        d = r.chat([{"role": "user", "content": a.text}])
        msg = d["choices"][0]["message"]
        print(f"供应商: {d['_provider']}")
        print(f"回复: {msg.get('content','')[:200]}")


if __name__ == "__main__":
    main()
