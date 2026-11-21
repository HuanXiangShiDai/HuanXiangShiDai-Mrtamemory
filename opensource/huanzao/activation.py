"""叠加层：近期增益。

它**不进图**——图是真相，增益是此刻的姿态。
被提到的元素抬一记；没被提到的，随对话一记一记慢慢回落。

回落按「轮」算，不按墙上的时间：她不是按秒活着，是按对话活着。
"""
from __future__ import annotations

import math
import sqlite3

DECAY_PER_TURN = 0.85     # 每轮不被提到，就掉 15%
BOOST = 1.0
CAP = 3.0
HALF_LIFE_TURNS = math.log(0.5) / math.log(DECAY_PER_TURN)   # ≈4.3 轮


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(
        "CREATE TABLE IF NOT EXISTS activation ("
        "  node_id INTEGER PRIMARY KEY, value REAL NOT NULL DEFAULT 0,"
        "  turn INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS turn_clock (id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  turn INTEGER NOT NULL DEFAULT 0);"
        "INSERT OR IGNORE INTO turn_clock(id, turn) VALUES(1, 0);")
    con.commit()


def turn(con: sqlite3.Connection) -> int:
    ensure(con)
    return int(con.execute("SELECT turn FROM turn_clock WHERE id=1").fetchone()["turn"])


def tick(con: sqlite3.Connection) -> int:
    """过一轮：全体回落一记。"""
    ensure(con)
    con.execute("UPDATE turn_clock SET turn = turn + 1 WHERE id=1")
    con.execute("UPDATE activation SET value = value * ?", (DECAY_PER_TURN,))
    con.execute("DELETE FROM activation WHERE value < 0.02")
    con.commit()
    return turn(con)


def bump(con: sqlite3.Connection, node_ids: list[int], amount: float = BOOST) -> None:
    """被提到：抬一记，封顶。"""
    if not node_ids:
        return
    ensure(con)
    now = turn(con)
    con.executemany(
        "INSERT INTO activation(node_id, value, turn) VALUES(?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET value = MIN(?, activation.value + ?), turn = ?",
        [(int(n), min(amount, CAP), now, CAP, amount, now) for n in node_ids])
    con.commit()


def values(con: sqlite3.Connection) -> dict[int, float]:
    ensure(con)
    return {int(r["node_id"]): float(r["value"])
            for r in con.execute("SELECT node_id, value FROM activation")}


def top(con: sqlite3.Connection, n: int = 10) -> list[tuple[str, float]]:
    ensure(con)
    rows = con.execute(
        "SELECT n.body, a.value FROM activation a JOIN nodes n ON n.id = a.node_id "
        "ORDER BY a.value DESC, n.body LIMIT ?", (n,)).fetchall()
    return [(str(r["body"]), float(r["value"])) for r in rows]

# ── 脉冲扩散（夜间窗口用）────────────────────────────────────────────
FANOUT = 6              # 每个点每跳最多传几个邻居
HOP_DECAY = 0.55        # 每跳衰减
MIN_NEXT = 0.08         # 低于这个电位不再传


def pulse_round(con: sqlite3.Connection, seeds: list[int], hops: int = 2,
                bump_positions: bool = True) -> dict:
    """从种子出发扩散一圈：只传两跳，按衰减落电位。

    和 `mg/diffuse.py` 的盲扩散不同——那个是**找记忆**（要求拼齐），
    这个是**抬姿态**（不问拼齐，只问谁被惊动了）。夜间激活要的是后者：
    让一夜之间没人碰过的元素里，离"最近在想的事"近的那些，先亮一档。
    """
    ensure(con)
    if not seeds:
        return {"seeds": 0, "reached": 0}

    pot: dict[int, float] = {int(s): 1.0 for s in seeds}
    frontier = set(pot)
    reached: set[int] = set()
    for _ in range(max(1, hops)):
        nxt: dict[int, float] = {}
        for cur in frontier:
            p = pot[cur]
            nb = con.execute(
                "SELECT dst, src FROM edges WHERE (src=? OR dst=?) "
                "AND rel NOT IN ('about','view','topic') LIMIT ?",
                (cur, cur, FANOUT)).fetchall()
            for r in nb:
                other = int(r["dst"]) if int(r["src"]) == cur else int(r["src"])
                if other in pot:
                    continue
                gain = p * HOP_DECAY
                if gain < MIN_NEXT:
                    continue
                if gain > nxt.get(other, 0):
                    nxt[other] = gain
        if not nxt:
            break
        pot.update(nxt)
        reached |= set(nxt)
        frontier = set(nxt)

    if bump_positions and reached:
        weights = sorted(reached, key=lambda n: -pot[n])[:24]
        for n in weights:
            bump(con, [n], amount=min(CAP, 0.5 * pot[n]))
    return {"seeds": len(seeds), "reached": len(reached),
            "top": sorted(((n, round(p, 3)) for n, p in pot.items()),
                          key=lambda x: -x[1])[:6]}
