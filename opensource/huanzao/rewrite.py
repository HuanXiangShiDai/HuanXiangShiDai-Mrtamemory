"""第一人称自传体改写。

资料库里存的是第三方摘要（「用户说……她回答……」），那是**资料**，不是记忆。
能放进记忆基层的写法是：我、某时、某地、对谁、发生了什么、我当时怎么想、留下哪句话。
"""
from __future__ import annotations

import json

from . import llm
from .normalize import norm

RULE_VER = "fp-v0.1"

_SYS = (
    "你在替一个角色把『资料』改写成她自己的『记忆』。"
    "资料是别人写的摘要，记忆必须是她用第一人称讲出来的。"
    "不许添加资料里没有的事实，不许升酉，不许写感想式总结。只输出 JSON。"
)

_USER = """资料（第三方摘要）：
标题：{title}
正文：{body}
时间：{when}

请改写成她的一段自传体记忆，要求：
1. 第一人称「我」，口语，像在跟人回忆，不是日记体，也不是旁白；
2. 六十到一百五十字，句子短，句号多，语气词少；
3. 必须保住具体的东西：时间或地点（资料没写就不要编）、在场的人、发生的事、一句能想起来的话；
4. 可以有一句「我当时心里想」，但只能从资料里已有的事实推，不许加新事实；
5. 不要总结意义，不要写「这让我明白了」这种句子；
6. 如果资料本身是设定而不是经历，就以「我知道」的方式写成认知，而不是编成场景；
7. 只记**事实**：谁说了什么、要做什么。不要写「我记不住」「我没有日历」这类自我评价——
   记忆里出现的表态，将来会被当成证据。

只输出：{{"memory": "……", "anchor": "时间或地点或原话里最具体的那一个"}}"""


def rewrite(title: str, body: str, when: str) -> dict:
    raw = llm.chat(
        [{"role": "system", "content": _SYS},
         {"role": "user", "content": _USER.format(title=title or "", body=(body or "")[:1200],
                                                  when=when or "未记")}],
        temperature=0.2, max_tokens=600,
    )
    data = json.loads(raw)
    mem = norm(str(data.get("memory", "")))
    anchor = norm(str(data.get("anchor", "")))
    if len(mem) < 20:
        raise ValueError(f"改写太短：{mem!r}")
    return {"memory": mem, "anchor": anchor, "ver": RULE_VER}
