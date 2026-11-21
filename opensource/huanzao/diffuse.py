"""从中心往外扩散，而不是从词往里查。

换的是方向：

    旧：对话里提到「惨剧」「约束」 → 去找含这两个元素的记忆
    新：从「中心人物」这个中心点扔进去 → 沿图扩散 → 看哪些记忆被**拼齐**

两条保证写在机制里：

1. **一致性**：所有激活都从中心出发，能被拼齐的记忆一定与中心相连
   （枕骨元素、先天认知、锚定记忆都在路径上），孤岛里的东西不会被捞出来；
2. **准确性**：要求「拼齐」——命中元素全在、且这条记忆的元素大部分都被扩散覆盖，
   拼不齐就不算命中，宁可这一轮不想起。
"""
from __future__ import annotations

import sqlite3

CENTER_SEED = 2.0
HOP_DECAY = 0.55
FANOUT = 8
MAX_TICKS = 6
FLOOR = 0.05            # 低于这个电位不再传
COVER_TARGET = 0.6      # 记忆的元素被覆盖到这个比例，才算「拼齐」
HIT_TARGET = 1.0        # 命中元素得全部落在里面


def _center(con: sqlite3.Connection) -> int | None:
    row = con.execute("SELECT id FROM nodes WHERE kind='center' LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _neighbours(con: sqlite3.Connection, nid: int, kind: str) -> list[tuple[int, float]]:
    """结构边一律 1.0；has 边按 1/该记忆的元素数（一条记忆摊给它的元素们）。

    优先级很重要：中心同时连着一堆 innate（设定）和 backbone（骨干元素），
    而 innate 那边是死路（设定不抽元素）。所以先走骨干、再走设定。
    """
    pri = {"axis": 0, "alias": 0, "backbone": 0, "innate": 2}
    out: list[tuple[int, float, int]] = []
    for r in con.execute(
        "SELECT dst, rel FROM edges WHERE src=? UNION SELECT src, rel FROM edges WHERE dst=?",
        (nid, nid)).fetchall():
        other, rel = int(r["dst"]), str(r["rel"] or "")
        w = 1.0
        if rel in ("has", "has:lead"):
            deg = con.execute(
                "SELECT COUNT(*) c FROM edges WHERE src=? AND rel IN ('has','has:lead')",
                (other,)).fetchone()["c"] or 1
            w = 1.0 / max(deg, 1)
        out.append((other, w, pri.get(rel, 1)))
    out.sort(key=lambda x: (x[2], -x[1]))
    return [(other, w) for other, w, _ in out[:FANOUT]]


def diffuse(con: sqlite3.Connection, hit_ids: list[int], ticks: int = MAX_TICKS) -> dict:
    center = _center(con)
    if center is None or not hit_ids:
        return {"memories": [], "arrived": {}, "ticks": 0}

    hit_set = set(int(h) for h in hit_ids)
    arrived: dict[int, int] = {center: 0}
    v: dict[int, float] = {center: CENTER_SEED}
    used = 0
    for tick in range(1, ticks + 1):
        used = tick
        spread: dict[int, float] = {}
        for nid, pot in list(v.items()):
            if pot < FLOOR:
                continue
            for nb, w in _neighbours(con, nid, ""):
                gain = pot * HOP_DECAY * w
                if gain < FLOOR:
                    continue
                if gain > spread.get(nb, 0.0):
                    spread[nb] = gain
        if not spread:
            break
        for nb, gain in spread.items():
            v[nb] = max(v.get(nb, 0.0), gain)
            arrived.setdefault(nb, tick)
        # 命中元素全到了就收手——「拼齐」是本算法的终点
        if hit_set <= set(arrived):
            break

    kinds = dict(con.execute("SELECT id, kind FROM nodes"))
    hit_set = set(int(h) for h in hit_ids)
    rows = con.execute(
        "SELECT src, dst FROM edges WHERE rel IN ('has','has:lead')").fetchall()
    mem_elems: dict[int, set[int]] = {}
    for r in rows:
        mem_elems.setdefault(int(r["src"]), set()).add(int(r["dst"]))

    scored = []
    for mem, elems in mem_elems.items():
        if kinds.get(mem) != "mem" or not elems:
            continue
        covered = len([e for e in elems if e in arrived])
        cover = covered / len(elems)
        hit_in = len([e for e in elems if e in hit_set])
        hit_cov = hit_in / max(len(hit_set), 1) if hit_set else 0.0
        hops = arrived.get(mem, 99)
        if cover < COVER_TARGET or (hit_set and hit_cov < HIT_TARGET):
            continue
        score = (cover ** 2) * (hit_cov if hit_set else 0.5) / (1 + hops)
        scored.append({"mem": mem, "cover": round(cover, 2), "hit": round(hit_cov, 2),
                       "hops": hops, "score": round(score, 3)})

    scored.sort(key=lambda x: -x["score"])
    return {"memories": scored, "arrived": len(arrived), "ticks": ticks, "center": center}


def _hops_from_center(con, center: int, limit: int = 4) -> dict:
    """纯 BFS，不做扇出——这里要的是「到得了吗」，不是「先想到谁」。

    `about` 不算一条路：它是「这段记忆说的是她」的标签，每段都挂着，
    算进来就等于所有记忆都在中心旁边，闸门再也拦不住任何东西。
    """
    adj = {}
    for r in con.execute(
            "SELECT src, dst FROM edges WHERE rel NOT IN ('about','view')").fetchall():
        a, b = int(r["src"]), int(r["dst"])
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen = {center: 0}
    frontier = [center]
    for hop in range(1, limit + 1):
        nxt = []
        for nid in frontier:
            for nb in adj.get(nid, ()):
                if nb not in seen:
                    seen[nb] = hop
                    nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    return seen


def pick(con, hit_ids, max_hops: int = 4, min_hit_cov: float = 0.5, top: int = 3) -> list:
    """候选取「含命中元素的记忆」，再用「离中心多远」做一致性校验。

    为什么不是纯扩散：实测过——从中心扩散六个 tick 只到 278 个点，
    「约束」这个元素根本没被传到。中心往外是「想到哪算哪」，不是「找得到」。
    """
    if not hit_ids:
        return []
    try:
        from .rules import center_rule
        rule = center_rule(con)
        max_hops = int(rule.get("max_hops", max_hops))
        min_hit_cov = float(rule.get("min_hit_cov", min_hit_cov))
    except Exception:
        pass
    hit_set = set(int(h) for h in hit_ids)
    center = _center(con)
    if center is None:
        return []
    dist = _hops_from_center(con, center, max_hops)
    idf = {int(r["node_id"]): float(r["idf"] or 1.0)
           for r in con.execute("SELECT node_id, idf FROM derived_df")}

    rows = con.execute("SELECT src, dst FROM edges WHERE rel IN ('has','has:lead')").fetchall()
    mem_elems = {}
    for r in rows:
        mem_elems.setdefault(int(r["src"]), set()).add(int(r["dst"]))

    out = []
    for mem, els in mem_elems.items():
        if not els or mem not in dist:
            continue
        its = hit_set & els
        if not its:
            continue
        hit_cov = len(its) / len(hit_set)
        if hit_cov < min_hit_cov:
            continue
        weight = sum(idf.get(e, 1.0) for e in its) / max(len(its), 1)
        score = hit_cov * (0.5 + 0.5 * len(its) / len(els)) * weight / (1 + dist[mem])
        out.append({"mem": mem, "hit_cov": round(hit_cov, 2), "hits": len(its),
                    "hops": dist[mem], "score": round(score, 3)})
    out.sort(key=lambda x: -x["score"])
    return out[:top]
