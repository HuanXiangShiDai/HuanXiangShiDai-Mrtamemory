"""这段第一人称叙事，那个「我」是谁。

语料是按「场景里每个角色」分别改写的，于是每段都有「我」——但「我」不一定是她。
不标出来，她会把渡鸦的拦截、维尔薇的解释，当成自己的往事。

判定用最稳的一条线索：**原文摘要的第一个专名**就是这段的主视角。
（摘要本来就是「渡鸦奉命阻止乙…」这样写的。）
"""
from __future__ import annotations

import sqlite3

from . import db

NAMES = [
    "中心人物", "乙", "丁", "维尔薇", "渡鸦", "甲", "丙", "千劫",
    "阿波尼亚", "苏", "华", "帕朵菲利斯", "帕朵", "格蕾修", "克莱茵", "灰蛇",
    "铃", "樱", "卡莲", "芬", "归藏", "萨拉", "薇塔", "琪亚娜",
]
SELF = "中心人物"


def guess(raw: str, fallback_title: str = "") -> str:
    """取原文摘要里第一个出现的专名。"""
    text = (raw or "")[:80]
    best, pos = "", len(text) + 1
    for name in NAMES:
        i = text.find(name)
        if 0 <= i < pos or (i == pos and name != SELF):
            # 「中心人物」出现在句首时优先（多半就是主视角）
            if i >= 0 and (i < pos or (i == pos and len(name) > len(best))):
                best, pos = name, i
    if best:
        return best
    # 原文没写名字，退回标题里找
    for name in NAMES:
        if name in (fallback_title or ""):
            return name
    return ""


def link_all(con: sqlite3.Connection, verbose: bool = True) -> dict:
    rows = con.execute(
        "SELECT mem_node, coalesce(raw,'') raw, coalesce(first_person,'') fp "
        "FROM mem_src WHERE order_key LIKE '2:%' OR order_key LIKE '1:%'").fetchall()
    dist: dict[str, int] = {}
    n = 0
    for r in rows:
        who = guess(r["raw"]) or guess(r["fp"]) or "未知"
        node = db.node(con, "person", who, uniq=f"person|{who}")
        db.edge(con, int(r["mem_node"]), node, "view")
        dist[who] = dist.get(who, 0) + 1
        n += 1
    con.commit()
    if verbose:
        print(f"标了 {n} 条视角归属")
        for who, k in sorted(dist.items(), key=lambda x: -x[1])[:12]:
            print(f"  {who}: {k}")
    return dist


def view_of(con: sqlite3.Connection, mem: int) -> str:
    row = con.execute(
        "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
        "WHERE e.src=? AND e.rel='view' LIMIT 1", (mem,)).fetchone()
    return str(row["body"]) if row else ""


REWRITE_SYS = (
    "你把一段「别人视角的第一人称叙述」改写成中心人物的回忆。\n"
    "要求：\n"
    "1. 用她的口吻转述这件事，把原文里的「我」换成那个人，并说明是她记得的；\n"
    "2. 事实、对话、顺序一个都不能改，不许添加原文没有的评价或情绪；\n"
    "3. 长度不超过原文，只输出改写后的正文，不要解释。"
)


def rewrite_batch(con, limit: int = 10, verbose: bool = True) -> dict:
    """把非她视角的记忆，改写成「她记得别人说过」的转述体。原文 raw 不动。"""
    from . import llm

    rows = con.execute(
        "SELECT s.mem_node, s.raw, s.first_person, n.body AS who "
        "FROM mem_src s JOIN edges e ON e.src=s.mem_node AND e.rel='view' "
        "JOIN nodes n ON n.id=e.dst "
        "WHERE n.body NOT IN ('中心人物','未知') "
        "AND coalesce(s.fp_ver,'') <> 'view-v2' "
        "ORDER BY s.mem_node LIMIT ?", (limit,)).fetchall()
    done = 0
    for r in rows:
        who = str(r["who"])
        src = str(r["first_person"] or r["raw"] or "")[:1200]
        try:
            out = llm.chat(
                [{"role": "system", "content": REWRITE_SYS},
                 {"role": "user", "content": f"叙述者：{who}\n\n{src}"}],
                temperature=0.2, max_tokens=600, json_mode=False)
        except Exception:
            continue
        text = " ".join(str(out or "").split())
        if len(text) < 20:
            continue
        con.execute("UPDATE mem_src SET first_person=?, fp_ver='view-v2' WHERE mem_node=?",
                    (text, int(r["mem_node"])))
        done += 1
        if verbose:
            print(f"  [{r['mem_node']}] {who} → {text[:46]}…")
    con.commit()
    left = con.execute(
        "SELECT COUNT(*) FROM mem_src s JOIN edges e ON e.src=s.mem_node AND e.rel='view' "
        "JOIN nodes n ON n.id=e.dst WHERE n.body NOT IN ('中心人物','未知') "
        "AND coalesce(s.fp_ver,'') <> 'view-v2'").fetchone()[0]
    return {"rewritten": done, "left": left}
