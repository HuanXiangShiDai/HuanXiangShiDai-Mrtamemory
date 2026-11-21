"""真正的增量生长：从零开始，按叙事顺序一片一片往上加。

和 `grow` 的区别：
- `grow` 是在「已经建好的图」上按切片重算，量的是「按时间看重心的形状」；
- 这里先清空边，再按切片逐片追加，每一片结束时的图**只包含它之前的记忆**。
  所以漂移量的是生长本身。

做法：把身份数据（点、原文、抽取结果）复制到一个新库，边一条不带；
然后按 timeline.slices 的顺序，一片一片地把 has 边补进去。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, grow, timeline

COPY_NODES = "SELECT id, kind, body, uniq FROM nodes"
COPY_SRC = ("SELECT mem_node, src_id, raw, first_person, fp_ver, when_iso, order_key, provenance "
            "FROM mem_src")
COPY_RUNS = "SELECT id, mem_node, mode, rule_ver, payload, created FROM extract_run"


def fork(src_path: Path, dst_path: Path) -> sqlite3.Connection:
    """把身份搬过去，边不带——边要在生长过程中一条条长出来。"""
    if dst_path.exists():
        dst_path.unlink()
    for extra in (Path(str(dst_path) + "-wal"), Path(str(dst_path) + "-shm")):
        if extra.exists():
            extra.unlink()

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    con = db.init(dst_path)

    con.executemany("INSERT INTO nodes(id, kind, body, uniq) VALUES(?,?,?,?)",
                    [(r["id"], r["kind"], r["body"], r["uniq"]) for r in src.execute(COPY_NODES)])
    con.executemany(
        "INSERT INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, when_iso, order_key, provenance) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [tuple(r) for r in src.execute(COPY_SRC)])
    con.executemany(
        "INSERT INTO extract_run(id, mem_node, mode, rule_ver, payload, created) VALUES(?,?,?,?,?,?)",
        [tuple(r) for r in src.execute(COPY_RUNS)])
    con.commit()
    src.close()
    return con


def add_mem_edges(con: sqlite3.Connection, mem: int) -> int:
    """给一条记忆补上它的 has 边（元素点按需新建）。"""
    row = con.execute(
        "SELECT payload FROM extract_run WHERE mem_node=? AND mode='llm' "
        "ORDER BY id DESC LIMIT 1", (mem,)).fetchone()
    if not row:
        return 0
    import json
    from .lexicon import AFFECT
    from .canon import canon

    n = 0
    for it in json.loads(row["payload"]):
        surface = canon(str(it.get("surface")))
        if not surface:
            continue
        kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
        el = db.node(con, kind, surface, uniq=f"{kind}|{surface}")
        if el == mem:
            continue
        rel = "has:lead" if it.get("role") == "lead" else "has"
        for _ in range(min(int(it.get("repeat") or 1), 3)):
            db.edge(con, mem, el, rel)
            n += 1
    return n


def grow_incremental(con: sqlite3.Connection, k_backbone: int = 120, chunk: int = 60,
                     verbose: bool = True) -> list[dict]:
    """一片一片地长。

    每「一步」的大小固定为 chunk 条记忆——不然一章 262 条一口气灌进去，
    漂移自然会飙高，那量的是「批次多大」，不是「长得稳不稳」。
    """
    con.execute("DELETE FROM edges")
    con.commit()
    grow.ensure_center(con)
    innate = grow.link_innate(con)
    if verbose:
        print(f"  先天认知直挂中心：{innate} 条；边从零开始；每步 {chunk} 条")

    out: list[dict] = []
    prev: dict[int, float] = {}
    active: set[int] = set()
    for name, mems in timeline.slices(con):
        for i in range(0, len(mems), chunk):
            step = mems[i:i + chunk]
            active |= set(step)
            added = 0
            for m in step:
                added += add_mem_edges(con, m)
            con.commit()
            grow.rebuild_df(con, active)
            n_back, cover = grow.link_backbone(con, k_backbone)
            ecc = grow.eccentricity(con, db.get_node(con, "center|self")["id"])
            cur = grow.snapshot(con)
            d = grow.drift(prev, cur)
            prev = cur
            label = name if len(mems) <= chunk else f"{name}#{i // chunk + 1}"
            rec = {"slice": label, "mems": len(step), "edges_added": added,
                   "backbone": n_back, "cover": cover, "ecc": ecc["mem_ecc"],
                   "elem_ecc": ecc["all_ecc"], "unreachable": ecc["unreachable"], "drift": d}
            out.append(rec)
            if verbose:
                print(f"  {label:<18} 记忆 {len(step):>3}  +边 {added:>5}  骨干 {n_back:>3}  "
                      f"覆盖 {cover*100:>5.1f}%  离心率 {ecc['mem_ecc']}/{ecc['all_ecc']}  "
                      f"孤立 {ecc['unreachable']:>4}  漂移 {d:.3f}")

    # 收尾：扫掉从没被用到的点，再量一次「真正到不了」的
    from .pipeline import sweep_orphans
    sweep_orphans(con)
    grow.rebuild_df(con, active)
    grow.link_backbone(con, k_backbone)
    ecc = grow.eccentricity(con, db.get_node(con, "center|self")["id"])
    final = {"mem_ecc": ecc["mem_ecc"], "all_ecc": ecc["all_ecc"],
             "unreachable": ecc["unreachable"], "mems": len(active),
             "backbone": con.execute(
                 "SELECT COUNT(*) c FROM edges WHERE rel='backbone'").fetchone()["c"]}
    if verbose:
        print(f"  收尾：离心率 {ecc['mem_ecc']}/{ecc['all_ecc']}  到不了 {ecc['unreachable']}")
    return out, final
