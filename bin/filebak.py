#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写文件前的「留后路」——统一备份 + 自动回收旧备份。

为什么要有这个模块
==================
2026-10-30 在 config/ 里翻出 **274 个** `pipeline.json.bak.20261029_*`（3.4M）：
同一个文件一天被改了几十次，每次都新建一个带时间戳的备份，谁也不负责清理。

这东西的坏处不只是占地方：
  · `ls config/` 一屏全是 .bak，真正要找的配置文件被淹掉；
  · 想"回滚到上一版"时，274 个候选里挑不出哪个是上一版 —— **备份多到等于没有备份**。

所以规则改成：**同一个文件最多留最近 N 个备份**（默认 5）。
要回滚，最近的 5 个足够；要考古，git 里有完整历史（本仓库是 git 管理的）。

以前的代码长这样（9 处一模一样的复制粘贴）：
    bak = path + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, bak)
现在统一调 `backup_file(path)`。
"""

import glob
import os
import shutil
from datetime import datetime

# 同一份文件保留几个备份。5 个是拍的：够回滚几次误操作，又不至于淹没目录。
DEFAULT_KEEP = 5


def bak_path(path, keep=DEFAULT_KEEP):
    """算出这次备份该叫什么名字，**顺手回收**超量的旧备份。

    ★ 为什么做成「取名时顺手回收」而不是「复制完再回收」：
      全项目有 20 处备份代码，形状各不相同（有的 `if isfile` 才复制、
      有的直接 `copy2(target, target + ".bak." + 时间戳)`）。
      统一替换成调本函数，就只动表达式、不动后面的 copy2 逻辑 ——
      改动面最小，不会因为手改 20 处而引入笔误。

    返回：`path.bak.<时间戳>`（还没创建，调用方自己 copy2 过去）。
    """
    # 给马上要新建的这一个腾位置，所以旧的最多留 keep-1 个
    _prune(path, max(keep - 1, 0))
    return path + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_file(path, keep=DEFAULT_KEEP):
    """一步到位版：复制 + 回收。原文件不存在则返回 None。

    新代码优先用这个；上面 `bak_path()` 是给存量 20 处准备的过渡形态。
    """
    if not os.path.isfile(path):
        return None
    bak = bak_path(path, keep)
    try:
        shutil.copy2(path, bak)
    except OSError:
        # 备份失败不该挡住正事（写文件本身才是目的），但也不能假装成功
        return None
    return bak


def _prune(path, keep):
    """回收超出 keep 的旧备份，只动 `path.bak.*` 这一族。"""
    if keep is None or keep < 0:
        return
    # ★ 用 glob 的 [] 通配转义而不是直接拼字符串：路径里有 [ ] * ? 时才不会误匹配
    pat = glob.escape(path) + ".bak.*"
    try:
        cands = [p for p in glob.glob(pat) if os.path.isfile(p)]
    except OSError:
        return
    if len(cands) <= keep:
        return
    # 按修改时间从新到旧排；时间相同用名字兜底，保证顺序稳定
    cands.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
    for old in cands[keep:]:
        try:
            os.remove(old)
        except OSError:
            # 删不掉就算了（可能有别的进程拿着），下次再回收，不影响主流程
            pass
