"""跨度归一：把「一百公里的结界」和「一百公里」收成同一个元素。

抽取时不动原文（payload 保持原样），只在**入图**和**比较**的时候归一。
"""
from __future__ import annotations

import re

from .lexicon import AFFECT

_PARTICLES = "的了着过地得"
_LEAD = ("那个", "这个", "一个", "一种", "那位", "这位", "他的", "她的", "我的", "你的")
_QUOTE = re.compile(r"[「『“\"《](.+?)[」』”\"》]")


def canon(surface: str) -> str:
    """一条元素的规范形。同一件事的不同切口，收敛到同一个写法。"""
    s = str(surface).strip()
    if not s:
        return s
    m = _QUOTE.fullmatch(s)
    if m:                      # 原话整段引用，原样保留
        return s
    # 1) 情绪以词表那一个词为准
    if s in AFFECT:
        return s
    for w in sorted(AFFECT, key=len, reverse=True):
        if w in s and len(s) > len(w):
            return w
    # 2) 去掉领属词
    for head in _LEAD:
        if s.startswith(head) and len(s) > len(head) + 1:
            s = s[len(head):]
            break
    # 3) 边界虚字
    s = s.strip(_PARTICLES)
    # 4) 「X的Y」：两边都够长时，取长的那一边（长的那边通常是这件事的中心）
    if "的" in s:
        left, _, right = s.partition("的")
        if len(left) >= 2 and len(right) >= 2:
            s = left if len(left) >= len(right) else right
        elif len(left) >= 2:
            s = left
        elif len(right) >= 2:
            s = right
    s = s.strip(_PARTICLES).strip()
    return s[:8] if len(s) > 8 else s


def related(a: str, b: str) -> bool:
    """两条元素是不是在说同一件事：相等，或互相包含（够长才算）。"""
    if a == b:
        return True
    if len(a) >= 2 and a in b:
        return True
    if len(b) >= 2 and b in a:
        return True
    return False
