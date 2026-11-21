"""沿元素链流动：从「用户」流向「喜欢」再流向「咖啡」。

和 `retrieve` 的区别，一句话：
- `retrieve` 是**反查**——哪个元素被提到，就去找含它的记忆；
- `flow` 是**正流**——把这句话里的元素按先后排成一条链，沿链往前走，
  每走到一个元素，就把它挂着的记忆点亮；两个相邻元素在同一条记忆里同时出现，那条记忆就亮得更狠。

三条规矩（按他的定义）：
1. **只处理元素**。正文不参与判定；记忆只在最后按 id 取原文。
2. **不在图里的词直接丢**，但要在 `missing` 里报出来——丢了什么必须看得见。
3. **权重不存，算出来**：用 idf（稀有的元素更值钱）和链上的位置算，不往 `edges` 里塞权重。

    python -m mg.pipeline flow --text "用户喜欢咖啡"
"""
from __future__ import annotations

import math
import sqlite3

from . import retrieve


def elements_in_order(con: sqlite3.Connection, text: str) -> tuple[list[int], list[str]]:
    """这句话里的元素，按在句子里出现的先后排。返回 (在图的, 不在图的)。

    两件要注意的：
    - **同一个词可能有好几个节点**（`person|丁` 和 `entity|丁` 是两张皮），按正文去重；
    - 「不在图里」的部分要能报出来：拿已知元素做正向最大匹配切句子，
      切不动的残片就是图外的词。整句报一句「用户喜欢咖啡」等于没报。
    """
    rows = con.execute(
        "SELECT id, body FROM nodes WHERE kind IN ('entity','affect','person') "
        "AND LENGTH(body) >= 2 ORDER BY LENGTH(body) DESC").fetchall()
    by_body: dict[str, int] = {}
    for r in rows:
        body = str(r["body"])
        if body in retrieve.NOISE or body in retrieve.META:
            continue
        by_body.setdefault(body, int(r["id"]))          # 长的优先，同一个词取一个
    found: list[tuple[int, int]] = []
    for body, nid in by_body.items():
        i = text.find(body)
        if i >= 0:
            found.append((i, nid))
    found.sort()

    # 正向最大匹配，把句子切成「元素 / 图外残片」
    words = sorted(by_body, key=len, reverse=True)
    missing: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        hit = next((w for w in words if text.startswith(w, i)), None)
        if hit:
            i += len(hit)
            if len(buf) >= 2:
                missing.append(buf)
            buf = ""
        else:
            ch = text[i]
            if ch in "，。！？、,.!? \t\n":
                if len(buf) >= 2:
                    missing.append(buf)
                buf = ""
            else:
                buf += ch
            i += 1
    if len(buf) >= 2:
        missing.append(buf)
    return [i for _, i in found], list(dict.fromkeys(missing))


def ensure_elements(con: sqlite3.Connection, text: str, words: list[str],
                    anchor: str = "用户") -> list[str]:
    """图里没有的词，建成新元素。

    三件事，缺一件这个新元素就是死的：
    1. 建点：`entity|<词>`；只增不改，重复调用不会长第二个。
    2. **挂锚**：连一条 `mentioned` 到「用户」——它是从**他嘴里**出来的，
       不挂锚的话它和图没有任何关系，下次也接不上。
    3. **串链**：和句子里相邻的元素连 `seq`，这样「用户→喜欢→咖啡」这条顺序
       下次还能走通。
    """
    from . import db
    made: list[str] = []
    for w in words:
        w = w.strip()
        if not (2 <= len(w) <= 6) or w in retrieve.NOISE or w in retrieve.META:
            continue
        if con.execute("SELECT 1 FROM nodes WHERE body=?", (w,)).fetchone():
            continue
        db.node(con, "entity", w, uniq=f"entity|{w}")
        made.append(w)
    if not made:
        return []
    anchor_row = con.execute(
        "SELECT id FROM nodes WHERE body=? AND kind IN ('entity','person') LIMIT 1",
        (anchor,)).fetchone()
    for w in made:
        row = con.execute("SELECT id FROM nodes WHERE body=? LIMIT 1", (w,)).fetchone()
        if not row:
            continue
        if anchor_row:                      # 从「他」嘴里出来的
            db.edge(con, int(anchor_row["id"]), int(row["id"]), "mentioned")
    con.commit()
    return made


def _link_seq(con: sqlite3.Connection, chain: list[int]) -> int:
    """把这条链按先后串成 seq 边（已存在就跳过）。"""
    from . import db
    n = 0
    for a, b in zip(chain, chain[1:]):
        if a != b:
            db.edge(con, a, b, "seq")
            n += 1
    con.commit()
    return n


def _mems_with(con: sqlite3.Connection, elem_ids: list[int]) -> dict[int, set[int]]:
    """每个元素挂着哪些记忆。"""
    out: dict[int, set[int]] = {}
    for e in elem_ids:
        out[e] = {int(r["src"]) for r in con.execute(
            "SELECT DISTINCT src FROM edges WHERE dst=? AND rel IN ('has','has:lead')", (e,))}
    return out


def _idf(con: sqlite3.Connection) -> dict[int, float]:
    total = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"] or 1
    out = {}
    for r in con.execute("SELECT node_id, df FROM derived_df"):
        df = int(r["df"]) or 1
        out[int(r["node_id"])] = math.log(total / df)
    return out


def _body(con: sqlite3.Connection, nid: int) -> str:
    row = con.execute("SELECT body FROM nodes WHERE id=?", (nid,)).fetchone()
    return str(row["body"]) if row else ""


def flow(con: sqlite3.Connection, text: str, limit: int = 6,
         create: bool = False) -> dict:
    chain, missing = elements_in_order(con, text)
    created: list[str] = []
    if create and missing:
        created = ensure_elements(con, text, missing)
        if created:
            chain, missing = elements_in_order(con, text)   # 建完重新排链
            _link_seq(con, chain)
    if not chain:
        return {"chain": [], "missing": missing, "created": created,
                "results": [], "note": "这句话里没有图上的元素"}

    idf = _idf(con)
    mems = _mems_with(con, chain)
    score: dict[int, float] = {}
    why: dict[int, dict] = {}

    # ① 每个元素点亮它自己的记忆
    for e in chain:
        w = idf.get(e, 1.0)
        for m in mems.get(e, ()): 
            score[m] = score.get(m, 0.0) + w * 0.6
            why.setdefault(m, {"hits": [], "ordered": True})["hits"].append(_body(con, e))

    # ② 相邻元素对：同一记忆里同时出现 → 加成；顺序一致 → 再加
    for a, b in zip(chain, chain[1:]):
        both = mems.get(a, set()) & mems.get(b, set())
        wa, wb = idf.get(a, 1.0), idf.get(b, 1.0)
        for m in both:
            score[m] = score.get(m, 0.0) + (wa + wb) * 0.9
            why.setdefault(m, {"hits": [], "ordered": True})["hits"].append(
                _body(con, a) + "→" + _body(con, b))
        # 顺序：这条记忆里 a 是否排在 b 前面
        for m in both:
            row = con.execute(
                "SELECT first_person, raw FROM mem_src WHERE mem_node=?", (m,)).fetchone()
            txt = str((row["first_person"] or row["raw"] or "") if row else "")
            pa, pb = txt.find(_body(con, a)), txt.find(_body(con, b))
            if pa >= 0 and pb >= 0 and pa < pb:
                score[m] = score.get(m, 0.0) + 0.4
            elif pa >= 0 and pb >= 0:
                why.setdefault(m, {"hits": [], "ordered": True})["ordered"] = False

    # ③ 链上元素越多命中、越稀有，越靠前
    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:limit]
    out = []
    for m, s in ranked:
        info = why.get(m, {"hits": [], "ordered": True})
        out.append({"mem": m, "score": round(s, 3),
                    "hits": list(dict.fromkeys(info["hits"]))[:4],
                    "ordered": info["ordered"],
                    "layer": retrieve.describe_when(con, m)})
    return {"chain": [_body(con, e) for e in chain], "missing": missing,
            "created": created, "results": out, "note": ""}
