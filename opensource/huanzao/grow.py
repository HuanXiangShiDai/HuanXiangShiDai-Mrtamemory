"""生长：造中心、切时间片、逐片成型、量离心率与漂移。"""
from __future__ import annotations

import math
import sqlite3
from collections import deque

from . import db

AXIOMS = [
    # 中心的三根轴：她为什么存在、她怕什么、她怎么选。
    # 开源版是可替换的通用示例——换成你自己人物的内核即可。
    "我在记事", "我怕忘记", "总得有人在",
]
CENTER_BODY = "中心人物"


ALIASES = [
    # 中心人物的别名，会写进头段的「别人叫我」。
    # 开源版留中性项——接入方换成自己的称呼即可。
    "中心人物", "我", "自己",
]


def ensure_center(con: sqlite3.Connection) -> int:
    """造中心。**按正文判重**——只有这个函数能保证 grow 不乱种一遍根。

    踩过两次：replay 里调了一次 ensure_center，中心就多出三条轴线
    （`axiom|我爱人类` 与 `axis|我爱人类` 内容一样、点不一样），自传里
    「我一直带着这几句」于是把三条生长素念了两遍。
    """
    c = db.node(con, "center", CENTER_BODY, uniq="center|self")
    have_axis = {str(r["body"]) for r in con.execute(
        "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
        "WHERE e.src=? AND e.rel='axis'", (c,))}
    for a in AXIOMS:
        if a in have_axis:
            continue
        n = db.node(con, "concept", a, uniq=f"axiom|{a}")
        db.edge(con, c, n, "axis")
    have_alias = {str(r["body"]) for r in con.execute(
        "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
        "WHERE e.src=? AND e.rel='alias'", (c,))}
    for a in ALIASES:
        if a in have_alias:
            continue
        n = db.node(con, "entity", a, uniq=f"alias|{a}")
        db.edge(con, c, n, "alias")
    con.commit()
    return c


def link_innate(con: sqlite3.Connection) -> int:
    """设定类（canon）不是经历，是「我知道的事」。它们直挂中心，不进元素抽取。"""
    c = db.node(con, "center", CENTER_BODY, uniq="center|self")
    rows = con.execute(
        "SELECT mem_node FROM mem_src WHERE when_iso = 'canon'").fetchall()
    n = 0
    for r in rows:
        db.edge(con, c, int(r["mem_node"]), "innate")
        n += 1
    con.commit()
    return n


def rebuild_df(con: sqlite3.Connection, active: set[int] | None = None) -> None:
    """文档频率：派生层，随时可重算。active 给定时只统计「已经活过」的那些记忆。"""
    if active is None:
        rows = con.execute(
            "SELECT dst, COUNT(DISTINCT src) df FROM edges WHERE rel IN ('has','has:lead') GROUP BY dst"
        ).fetchall()
        total = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"] or 1
    else:
        if not active:
            con.execute("DELETE FROM derived_df")
            con.commit()
            return
        marks = ",".join(str(int(x)) for x in active)
        rows = con.execute(
            f"SELECT dst, COUNT(DISTINCT src) df FROM edges WHERE rel IN ('has','has:lead') "
            f"AND src IN ({marks}) GROUP BY dst"
        ).fetchall()
        total = len(active)
    con.execute("DELETE FROM derived_df")
    con.executemany(
        "INSERT INTO derived_df(node_id, df, total, idf) VALUES(?,?,?,?)",
        [(int(r["dst"]), int(r["df"]), total,
          math.log(total / r["df"]) if r["df"] > 0 else 0.0) for r in rows],
    )
    con.commit()


def backbone(con: sqlite3.Connection, k: int = 120, target: float = 1.0) -> list[int]:
    """骨干元素：不是「出现最多」，而是「能带住最多记忆」。

    这是个贪心集合覆盖——每挑一个元素，都要新盖住尽可能多的记忆。
    理由只有一条：离心率 ≤3 要求绝大多数记忆都能在两跳内被中心的骨干碰到。
    """
    rows = con.execute(
        "SELECT dst AS el, src AS mem FROM edges WHERE rel IN ('has','has:lead')").fetchall()
    cover: dict[int, set[int]] = {}
    total_mems = set()
    for r in rows:
        cover.setdefault(int(r["el"]), set()).add(int(r["mem"]))
        total_mems.add(int(r["mem"]))
    if not total_mems:
        return []
    need = len(total_mems) * target
    covered: set[int] = set()
    chosen: list[int] = []
    while len(chosen) < k and len(covered) < need:
        best, gain = None, 0
        for el, mems in cover.items():
            if el in chosen:
                continue
            g = len(mems - covered)
            if g > gain:
                best, gain = el, g
        if best is None or gain == 0:
            break
        chosen.append(best)
        covered |= cover[best]
    return chosen


def link_backbone(con: sqlite3.Connection, k: int = 120) -> tuple[int, float]:
    c = ensure_center(con)
    con.execute("DELETE FROM edges WHERE rel='backbone'")
    els = backbone(con, k)
    covered: set[int] = set()
    for el in els:
        db.edge(con, c, el, "backbone")
        for r in con.execute(
            "SELECT DISTINCT src FROM edges WHERE dst=? AND rel IN ('has','has:lead')", (el,)):
            covered.add(int(r["src"]))
    con.commit()
    mems = con.execute(
        "SELECT COUNT(DISTINCT src) c FROM edges WHERE rel IN ('has','has:lead')").fetchone()["c"] or 1
    return len(els), len(covered) / mems


# 「关于」不是一条路。它只是「这段记忆说的是她」，每条记忆都挂着一条，
# 算进可达性就等于把每段记忆直接塞到中心旁边——离心率恒等于 1，闸门永远放行。
# 量距离时一律不走它（见 2026-11-19 体检：加了 about 之后 ecc 从 2 掉到 1）。
SKIP_RELS = ("about",)


def eccentricity(con: sqlite3.Connection, start: int, limit: int = 60000) -> dict:
    """从中心出发的 BFS，分别看「记忆」和「全部节点」最远几跳。

    元素离心率天然比记忆多一跳：记忆 → 它自己的注脚元素。
    所以判据落在记忆上（≤3），元素作为参考一并报出来。
    """
    skip = "(" + ",".join("'%s'" % r for r in SKIP_RELS) + ")"
    seen = {start: 0}
    q = deque([start])
    while q:
        cur = q.popleft()
        d = seen[cur]
        if d > 12:
            continue
        for r in con.execute(
                f"SELECT src, dst FROM edges WHERE (src=? OR dst=?) AND rel NOT IN {skip}",
                (cur, cur)):
            nxt = r["dst"] if r["src"] == cur else r["src"]
            if nxt not in seen:
                seen[nxt] = d + 1
                q.append(nxt)
                if len(seen) > limit:
                    return {"mem_ecc": 99, "all_ecc": 99, "unreachable": 0, "reached": len(seen)}

    kinds = dict(con.execute("SELECT id, kind FROM nodes"))
    mem_ecc = max((d for n, d in seen.items() if kinds.get(n) == "mem"), default=0)
    all_ecc = max(seen.values(), default=0)
    total = len(kinds)
    return {"mem_ecc": mem_ecc, "all_ecc": all_ecc,
            "unreachable": total - len(seen), "reached": len(seen)}


def snapshot(con: sqlite3.Connection) -> dict[int, float]:
    """这一片结束时的重心：各元素的占比。"""
    rows = con.execute("SELECT node_id, df FROM derived_df WHERE df > 0").fetchall()
    tot = sum(r["df"] for r in rows) or 1
    return {int(r["node_id"]): r["df"] / tot for r in rows}


def drift(a: dict[int, float], b: dict[int, float]) -> float:
    """两版重心的差异：1 - 余弦。0 表示一点没动。"""
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return 1.0 - dot / (na * nb)


def slices(con: sqlite3.Connection) -> list[tuple[str, list[int]]]:
    """优先用叙事时间（timeline）；还没算过就退回按入库日切片。"""
    from . import timeline
    has = con.execute(
        "SELECT COUNT(*) c FROM mem_src WHERE order_key IS NOT NULL").fetchone()["c"]
    if has:
        return timeline.slices(con)
    rows = con.execute("SELECT mem_node, when_iso FROM mem_src ORDER BY when_iso, mem_node").fetchall()
    groups: dict[str, list[int]] = {}
    for r in rows:
        w = str(r["when_iso"] or "")
        key = "canon" if w == "canon" else (w[:10] or "未定")
        groups.setdefault(key, []).append(int(r["mem_node"]))
    order = sorted(groups, key=lambda k: (k != "canon", k))
    return [(k, groups[k]) for k in order]


def grow(con: sqlite3.Connection, k_backbone: int = 24, verbose: bool = True) -> list[dict]:
    """逐片注入：每片结束后重算重心、连骨干、量离心率与漂移。"""
    ensure_center(con)
    innate = link_innate(con)
    if verbose and innate:
        print(f"  先天认知（设定类）直挂中心：{innate} 条")
    out: list[dict] = []
    prev: dict[int, float] = {}
    active: set[int] = set()
    for name, mems in slices(con):
        active |= set(mems)
        rebuild_df(con, active)
        n_back, cover = link_backbone(con, k_backbone)
        ecc = eccentricity(con, db.get_node(con, "center|self")["id"])
        cur = snapshot(con)
        d = drift(prev, cur)
        prev = cur
        rec = {"slice": name, "mems": len(mems), "backbone": n_back, "cover": cover,
               "ecc": ecc["mem_ecc"], "elem_ecc": ecc["all_ecc"],
               "unreachable": ecc["unreachable"], "drift": d}
        out.append(rec)
        if verbose:
            print(f"  {name:<14} 记忆 {len(mems):>3}  骨干 {n_back:>2}  覆盖 {cover*100:>5.1f}%  "
                  f"离心率(记忆) {ecc['mem_ecc']:>2} / (元素) {ecc['all_ecc']:>2}  "
                  f"孤立 {ecc['unreachable']:>3}  漂移 {d:.3f}")
    return out
