"""客户内容能不能离开这台机器 —— 一条策略，所有调模型的入口共用。

## 为什么单独一个模块

`card.json` 里的 `policy.allow_cloud_llm` 一直是**唯一真值**，RUNBOOK 里也写着
「客户档案默认禁止内容上云（走本地 9B）；本人 IP 才是 true」。
但 2026-10-29 发现：没有任何代码去读它。`studio_ai.py`、`prompts.py` 都直接起
`LLMRouter(sensitive=True)` —— sensitive 解决的是「不送免费池」，**不等于不出本机**。
于是演示客户（type=客户、allow_cloud_llm=false）的人设和文案，真的被送到了云端模型。

政策写在文档里而代码不读，等于没有。所以抽成一个模块，谁要调模型谁先问它。

## 三条出路（报错信息里会原样告诉人）

1. 这个 IP 确实可以上云 → 把 `card.json` 的 `policy.allow_cloud_llm` 改成 true
   （本人 IP 就是 true；改完在 changelog 里有记录）
2. 只是这一次想放行 → 跑命令时加环境变量 `STUDIO_ALLOW_CLOUD=1`
3. 客户内容一律不出本机 → 等本地模型接进路由（RUNBOOK 待办第 ③ 项）

默认是 **fail closed**：读不到 card.json、读不到 policy，都按「不许上云」处理。
宁可让 AI 功能报错停下，也不要把客户的商业内容送出这台机器。
"""
import json
import os

ENV_OVERRIDE = "STUDIO_ALLOW_CLOUD"


def _load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def card_policy(ip_root):
    """返回 (allow: bool, why: str)。读不到一律 False。"""
    if not ip_root:
        return False, "没有档案目录，按客户处理"
    card = _load_json(os.path.join(ip_root, "card.json"))
    if card is None:
        return False, "读不到 card.json，按客户处理"
    pol = card.get("policy") or {}
    if "allow_cloud_llm" not in pol:
        return False, "card.json 没写 policy.allow_cloud_llm，按默认不许上云"
    v = pol.get("allow_cloud_llm")
    return bool(v), ("卡里写明允许上云" if v else "卡里写明禁止上云")


def cloud_allowed(ip_root, who="这个 IP"):
    """能不能把内容送去云端模型。返回 (ok, reason)。

    环境变量放行会**明确记一笔**在 reason 里，让调用方有机会打出来 ——
    偷偷放行比拒绝更危险：没人知道自己破了规矩。
    """
    allow, why = card_policy(ip_root)
    if allow:
        return True, why
    if os.environ.get(ENV_OVERRIDE) == "1":
        return True, f"{why}；但设了 {ENV_OVERRIDE}=1 临时放行（这次内容会出本机）"
    return False, why


class CloudBlocked(RuntimeError):
    """客户内容不许上云。消息本身就是给人看的说明书，别再包一层。"""

    def __init__(self, who, why):
        self.who = who
        self.why = why
        super().__init__(
            f"「{who}」的内容不能送出这台机器（{why}）。\n"
            f"  这份人设/文案是客户的商业信息，送到云端模型等于交出去。\n"
            f"  三条路，选一条：\n"
            f"    1) 这个 IP 本来就可以上云 → 改 card.json 的 policy.allow_cloud_llm 为 true\n"
            f"    2) 只是这一次要放行 → 命令前加 {ENV_OVERRIDE}=1\n"
            f"    3) 客户内容一律不出本机 → 等本地模型接进路由（RUNBOOK 待办 ③）"
        )


def require_cloud(ip_root, who="这个 IP"):
    """不让上云就抛 CloudBlocked。调用方直接让异常冒到 main 打印即可。"""
    ok, why = cloud_allowed(ip_root, who)
    if not ok:
        raise CloudBlocked(who, why)
    return why
