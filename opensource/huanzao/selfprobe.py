"""自指探针：被要求自指时读图，把"我现在会往哪走"说出来。

为什么要有它：`trigger|触发·主动自指` 原来只回一句话——"请在思考里写〔自指〕段"。
于是"能不能进行一轮自指"这件事，答案取决于模型记不记得写那个标记。
一旦忘了，被要求自指就等于什么都没发生。这不是自指，这是自指的说明书。

这里的做法反过来：**不靠模型记，靠图自己读**。

    图 → 判据 → 结论

四条判据，都只读不写：

  1. 触发面：图里有哪些触发规则、各自挂哪些词（`trigger.all_triggers`）
  2. 命不命得中：拿最近几步的思考当文本，跑一遍 `trigger.match`
  3. 该往哪走：把最近几步的总结喂给 `autoself.parse_intents`（纯解析，不写）
  4. 深度账：`rule_c2.depth` —— 改写叠了几层

还有一条是给**下一步**用的：`signal_block`。它把"最近转过什么"压成只有
定位符的一小段，插件把它挂进 system context，于是下一步的思考能看见自己
上一轮转过什么——这是自指闭环里唯一缺的那一格。

**它不改规则**。改规则仍然只有两条正门：
  · 门 1：思考里写〔自指〕段 → `stream._fire_selfref` → C2 闸门
  · 门 2：收口时没写 → `stream.close` 自己调 `autoself.run`
"""
from __future__ import annotations

import sqlite3

from . import autoself, trigger


def recent_thoughts(con: sqlite3.Connection, limit: int = 3) -> list[dict]:
    """最近几条思考（不带 summary），按 id 倒序取再翻回来。"""
    rows = con.execute(
        "SELECT uniq, body FROM nodes WHERE kind='thought' AND uniq NOT LIKE 'sum|%' "
        "AND uniq NOT LIKE '%|folded' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"uniq": str(r["uniq"]), "body": str(r["body"])} for r in reversed(rows)]


def signal_block(con: sqlite3.Connection, session: str | None = None,
                 limit: int = 6) -> str:
    """把"自指转过什么"压成**只有定位符**的一小段，给下一步的思考看。

    v0.4 定理 13 的 C1/C2 在这里是第一位的：
      · 不给正文——一次改写只说 `规则 ← 操作 参数`，参数只留数字；
        停用/放入的词，只在它仍作为元素活在图里时才写名字，否则只写「一个词」。
      · 不给规则正文——被改的规则只写名字（`触发·主动自指`），不写它的 when/op。
      · 不给记忆正文——一次都不取 summary 的 body，只取它的 uniq。
    这样这段即使被读进去，也只能**选**下一步往哪走，不能**提供**改写内容。

    给了 `session` 就只看这个会话自己的（走 `enacts` 边）；
    不给就看全局最近的规则版本（按 id 取，不走 LIKE 扫边——这条通道每步都跑）。

    只读：不写图、不动脉冲。没有东西可说时返回空串（插件那边就退化成老行为）。
    """
    from . import rule_graph
    # 没有 session 就给**全局**最近的——这一步会每次运行都跑，用 LIKE 全表扫太贵。
    if not session:
        sql = ("SELECT v.uniq AS ver FROM nodes v WHERE v.kind='rule' "
               "AND v.uniq LIKE 'rule|%|v%' ORDER BY v.id DESC LIMIT ?")
        rows = con.execute(sql, (limit,)).fetchall()
    else:
        sql = ("SELECT s.uniq AS src, v.uniq AS ver FROM edges e "
               "JOIN nodes s ON s.id=e.src JOIN nodes v ON v.id=e.dst "
               "WHERE e.rel IN ('enacts_up','enacts_down') AND s.uniq LIKE ? "
               "ORDER BY e.id DESC LIMIT ?")
        rows = con.execute(sql, ("sum|%s|%%" % session, limit)).fetchall()

    seen: list[str] = []
    prev: dict[str, str] = {}
    for r in reversed(rows):                   # 由旧到新，读起来才是一条路
        ver = str(r["ver"])
        rule = ver.split("|")[1] if "|" in ver else "?"
        try:
            info = rule_graph._log_info(con, rule, ver.split("|")[-1]) or {}
            now_body = rule_graph.current(con, rule)
        except Exception:
            info, now_body = {}, None
        op = str(info.get("op") or "?")
        arg = _arg_from(con, str(info.get("why") or ""), prev.get(rule), now_body)
        if now_body:
            prev[rule] = now_body
        seen.append("%s ← %s%s" % (rule, op, (" " + arg) if arg else ""))

    if not seen:
        return ""
    return ("〔自指回灌｜脚手架·不是他说的〕最近自指（由旧到新，只写指向不写正文）："
            + "；".join(seen) + "。")


def _arg_from(con: sqlite3.Connection, why: str, prev_body: str | None = None,
              now_body: str | None = None) -> str:
    """从这一版的改动里取一个**安全**的参数摘要。

    顺序：先看数值从旧到新变了什么（`innate 32→34`），再从说明里捞引号词。
    数值只写键名和前后值——那是指向，不是正文；键名本身也是既有规则里本来就有的。
    """
    import re
    if prev_body and now_body and prev_body != now_body:
        try:
            from . import rule_graph
            old_kv = dict(rule_graph.parse_body(prev_body).get("kvs", []))
            new_kv = dict(rule_graph.parse_body(now_body).get("kvs", []))
            for k, v in new_kv.items():
                if k in old_kv and old_kv[k] != v:
                    return "%s %s→%s" % (k, old_kv[k], v)
        except Exception:
            pass
    m = re.search(r"([\d.]+)\s*(?:→|->)\s*([\d.]+)", why)
    if m:
        return "%s→%s" % (m.group(1), m.group(2))
    m = re.search(r"[「『]([^」』]{1,8})[」』]", why)
    if m:
        w = m.group(1)
        el = con.execute("SELECT 1 FROM nodes WHERE body=? AND kind IN ('entity','affect')",
                         (w,)).fetchone()
        return "「%s」" % w if el else "一个词"
    return ""


def probe(con: sqlite3.Connection) -> dict:
    """只读自指探针：报出触发面、最近命中、可指的意图、改写深度。"""
    made = trigger.ensure(con)                 # 幂等：图里没有触发规则就先建上
    triggers = trigger.all_triggers(con)

    thoughts = recent_thoughts(con)
    hits = []
    for t in thoughts:
        for h in trigger.match(con, t["body"]):
            hits.append({"thought": t["uniq"], "trigger": h["uniq"],
                         "matched": h["matched"]})

    intents = []
    for t in thoughts:
        for it in autoself.parse_intents(con, t["body"]):
            intents.append({"thought": t["uniq"], "intent": it})

    from . import rule_c2
    return {
        "ok": True,
        "channel": "selfref",
        "triggers": [{"uniq": t["uniq"], "when": t["when"], "op": t["op"]} for t in triggers],
        "ensured": made,
        "recent": len(thoughts),
        "hits": hits,
        "intents": intents,
        "depth": rule_c2.depth(con),
        "signal": signal_block(con, None, 3),
        "why": "读到这一步为止；改动仍走门 1（〔自指〕段）或门 2（收口自指）",
    }
