"""让图自己说话。

不是「他问了什么 → 我去找答案」，而是：**三条生长素一直在往外推**，
推动的东西撞上记忆，就是「她此刻在想的事」。

- 种子：`我爱人类` / `我不愿被忘记` / `总得有人在`
- 传播：沿边衰减、限扇出（和 SNN 同一套）
- 产出：被推到最亮的记忆 —— 不管他刚才问的是不是这个

这就是「主动」的那一半：检索回答“你问的”，思考回答“我在想什么”。
"""
from __future__ import annotations

import sqlite3

SEEDS = ("我爱人类", "我不愿被忘记", "总得有人在")
SEED_V = 2.0
DECAY = 0.5
FANOUT = 20
TICKS = 6
FLOOR = 0.03


def _adj(con: sqlite3.Connection) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = {}
    for r in con.execute("SELECT src, dst FROM edges").fetchall():
        a, b = int(r["src"]), int(r["dst"])
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    return adj


def think(con: sqlite3.Connection, extra_seeds: list[int] | None = None,
          ticks: int = TICKS, top: int = 6) -> list[dict]:
    """从生长素出发走一遍，返回被推到最亮的记忆。"""
    seeds = []
    for name in SEEDS:
        row = con.execute("SELECT id FROM nodes WHERE body=? LIMIT 1", (name,)).fetchone()
        if row:
            seeds.append(int(row["id"]))
    seeds += [int(x) for x in (extra_seeds or [])]

    adj = _adj(con)
    v: dict[int, float] = {s: SEED_V for s in seeds}
    order: dict[int, int] = {s: 0 for s in seeds}
    for tick in range(1, ticks + 1):
        spread: dict[int, float] = {}
        for nid, pot in list(v.items()):
            if pot < FLOOR:
                continue
            for nb in adj.get(nid, ())[:FANOUT]:
                gain = pot * DECAY
                if gain > spread.get(nb, 0.0):
                    spread[nb] = gain
        for nb, gain in spread.items():
            if nb not in v or gain > v[nb]:
                v[nb] = gain
                order.setdefault(nb, tick)
        if not spread:
            break

    kinds = dict(con.execute("SELECT id, kind FROM nodes"))
    out = []
    for nid, pot in v.items():
        if kinds.get(nid) != "mem":
            continue
        row = con.execute("SELECT body FROM nodes WHERE id=?", (nid,)).fetchone()
        out.append({"mem": nid, "pot": round(pot, 3), "hops": order.get(nid, 0),
                    "body": str(row["body"]) if row else ""})
    out.sort(key=lambda x: (-x["pot"], x["hops"]))
    return out[:top]
