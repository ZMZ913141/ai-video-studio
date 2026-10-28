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
             temperature: float = 0.2, verbose: bool = True) -> dict:
        """依次尝试所有可用供应商；全部失败才抛异常。"""
        body_base = {"messages": messages, "max_tokens": max_tokens,
                     "temperature": temperature}
        if tools:
            body_base["tools"] = tools
        if tool_choice:
            body_base["tool_choice"] = tool_choice
        if response_format:
            body_base["response_format"] = response_format

        if not self.providers:
            raise RuntimeError("providers.json 里没有任何 enabled=true 的供应商")

        errors = []
        for attempt in range(2):          # 第二轮：强制解除最早到期的冷却再试
            for p in self.providers:
                if attempt == 0 and not self._available(p):
                    continue
                body = dict(body_base, model=p["model"])
                # 每家可自带 extra_body（例：智谱 "thinking": {"type":"disabled"}）
                if p.get("extra_body"):
                    body.update(p["extra_body"])
                t0 = _now()
                try:
                    d = self._call(p, body)
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

    # ---------------------------------------------------------------- 运维
    def status(self) -> list[dict]:
        rows = []
        for p in self.cfg["providers"]:
            key = os.environ.get(p.get("key_env", ""), "")
            st = self.state.get(p["name"], {})
            cooling = bool(st.get("until")) and st["until"] > _now()
            rows.append({
                "name": p["name"], "enabled": p.get("enabled", False),
                "priority": p.get("priority"), "model": p.get("model"),
                "key": ("已配置" if key else "缺 key"),
                "state": ("冷却至 " + _fmt(st["until"])) if cooling else "可用",
                "reason": st.get("reason", ""),
            })
        return rows

    def reset(self):
        self.state = {}
        self._save()


def main():
    ap = argparse.ArgumentParser(description="多供应商 LLM 路由")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "chat", "reset"])
    ap.add_argument("text", nargs="?", default="回复四个字：路由正常")
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
            print(f"  {x['priority']:>4}  {x['name']:<12} {x['key']:<7} "
                  f"{x['state']:<18} {x['model']}{tail}")
        dis = [x["name"] for x in r.status() if not x["enabled"]]
        print(f"\n  未启用（注册后打开）: {', '.join(dis)}")
    elif a.cmd == "reset":
        r.reset()
        print("✓ 已清空所有冷却状态")
    else:
        d = r.chat([{"role": "user", "content": a.text}])
        msg = d["choices"][0]["message"]
        print(f"供应商: {d['_provider']}")
        print(f"回复: {msg.get('content','')[:200]}")


if __name__ == "__main__":
    main()
