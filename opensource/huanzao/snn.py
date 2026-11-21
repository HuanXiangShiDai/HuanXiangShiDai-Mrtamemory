"""脉冲层：她想起一件事，是因为相连的东西先亮了。

这一层不进图。图是真相，脉冲是此刻的电。

模型是 LIF（正是 SNN 的神经元）：
    V ← V·exp(-Δt/τ) + 输入
    V ≥ θ  → 点亮，V 归零，进不应期

外加三条，让「忽然想起」有形状：
- 传播：一跳衰减 0.5，每个点只走最强的 4 个邻居
- 侧抑制：点亮的点给邻居一记负电流，防整张图一起亮
- 首达优先：越早到达的分量越重，特异的元素传得快、泛词传得慢
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

TICKS = 6
DECAY_PER_HOP = 0.5
FANOUT = 4
THETA = 0.6
SEED_V = 1.6
INHIBIT = -0.35
TAIL_TICKS = 3.0          # 首达衰减的时间常数（以 tick 计）
REFRACTORY = timedelta(hours=12)
NOISE = 0.05


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(
        "CREATE TABLE IF NOT EXISTS snn_state ("
        "  node_id INTEGER PRIMARY KEY, v REAL NOT NULL DEFAULT 0, last_spike TEXT);")
    con.commit()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _refractory(con: sqlite3.Connection, node_id: int) -> bool:
    row = con.execute("SELECT last_spike FROM snn_state WHERE node_id=?", (node_id,)).fetchone()
    if not row or not row["last_spike"]:
        return False
    try:
        last = datetime.fromisoformat(str(row["last_spike"]))
    except ValueError:
        return False
    return datetime.now().astimezone() - last < REFRACTORY


def _load_v(con: sqlite3.Connection) -> dict[int, float]:
    return {int(r["node_id"]): float(r["v"])
            for r in con.execute("SELECT node_id, v FROM snn_state")}


def _save_v(con: sqlite3.Connection, vs: dict[int, float], spiked: dict[int, str]) -> None:
    rows = [(nid, v, spiked.get(nid)) for nid, v in vs.items()]
    con.executemany(
        "INSERT INTO snn_state(node_id, v, last_spike) VALUES(?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET v=excluded.v, "
        "last_spike=COALESCE(excluded.last_spike, snn_state.last_spike)", rows)
    con.commit()


def df_of(con: sqlite3.Connection, node_id: int) -> int:
    row = con.execute(
        "SELECT COUNT(DISTINCT src) c FROM edges WHERE dst=? AND rel IN ('has','has:lead')",
        (node_id,)).fetchone()
    return int(row["c"] or 0)


def neighbours(con: sqlite3.Connection, node_id: int, fanout: int = FANOUT) -> list[tuple[int, float]]:
    """元素 → 跟它一起出现在记忆里的元素（按共现算强度，只取最强的几个）。"""
    mems = [int(r["src"]) for r in con.execute(
        "SELECT DISTINCT src FROM edges WHERE dst=? AND rel IN ('has','has:lead') LIMIT 60",
        (node_id,))]
    if not mems:
        return []
    marks = ",".join(str(m) for m in mems)
    rows = con.execute(
        f"SELECT dst, COUNT(*) c FROM edges WHERE src IN ({marks}) AND rel IN ('has','has:lead') "
        f"AND dst <> ? GROUP BY dst", (node_id,)).fetchall()
    if not rows:
        return []
    base = math.sqrt(max(df_of(con, node_id), 1))
    out = []
    for r in rows:
        el, co = int(r["dst"]), int(r["c"])
        w = co / (base * math.sqrt(max(df_of(con, el), 1)))
        out.append((el, w))
    out.sort(key=lambda x: -x[1])
    out = out[:fanout]
    if not out:
        return []
    # 归一：最亲的那条算 1.0。不归一的话，图的密度会把传播整个吃掉——
    # 边权普遍小于 1，乘以 0.5 之后永远到不了阈值，那「忽然想起」就永远是零。
    top = out[0][1] or 1.0
    return [(el, w / top) for el, w in out]


def run(con: sqlite3.Connection, seed_ids: list[int], ticks: int = TICKS) -> dict:
    """注入这批种子，跑几个 tick，返回点亮了谁。"""
    ensure(con)
    vs = _load_v(con)
    spiked_at: dict[int, int] = {}
    history: dict[int, str] = {}
    arrivals: dict[int, int] = {}

    for sid in seed_ids:
        vs[sid] = min(vs.get(sid, 0.0) + SEED_V, SEED_V * 2)

    lit: list[dict] = []
    for tick in range(ticks):
        # 传播：只从「有电位」的点往外走
        fresh: dict[int, float] = {}
        for nid, v in list(vs.items()):
            if v <= NOISE:
                continue
            for nb, w in neighbours(con, nid):
                fresh[nb] = fresh.get(nb, 0.0) + v * w * DECAY_PER_HOP

        for nid, add in fresh.items():
            if nid not in arrivals:
                arrivals[nid] = tick
            vs[nid] = vs.get(nid, 0.0) + add

        # 阈值 + 侧抑制
        for nid, v in list(vs.items()):
            if v < THETA:
                continue
            if _refractory(con, nid):
                vs[nid] = 0.0
                continue
            vs[nid] = 0.0
            spiked_at[nid] = tick
            history[nid] = _now()
            for nb, w in neighbours(con, nid):
                vs[nb] = vs.get(nb, 0.0) + INHIBIT * w

        # 漏电（每个 tick 都掉一点）
        for nid in list(vs):
            vs[nid] *= 0.85
            if abs(vs[nid]) < NOISE:
                vs.pop(nid, None)

    seed_set = set(seed_ids)
    for nid, tick in spiked_at.items():
        hops = 1 + arrivals.get(nid, tick)
        weight = (1.0 / hops) * math.exp(-tick / TAIL_TICKS)
        row = con.execute("SELECT body, kind FROM nodes WHERE id=?", (nid,)).fetchone()
        lit.append({"id": nid, "body": row["body"] if row else "", "kind": row["kind"] if row else "",
                    "tick": tick, "hops": hops, "score": round(weight, 3),
                    "seed": nid in seed_set})

    lit.sort(key=lambda x: -x["score"])
    _save_v(con, vs, history)
    return {"lit": lit, "spikes": len(lit), "seeds": len(seed_ids)}


def describe(con: sqlite3.Connection, seed_ids: list[int]) -> str:
    got = run(con, seed_ids)
    if not got["lit"]:
        return ""
    heads = "、".join(f"{x['body']}({x['hops']}跳)" for x in got["lit"][:4])
    return heads
