"""写入即入图：归档的那一刻就把这件事做完，不留到以后。

一次写入做四件事：
1. 改写成第一人称（自传体记忆，不是第三方摘要）；
2. 抽元素——**全过程**都抽，不按上限砍（写入是宽进，检索才是严出）；
3. 连边、排进时间轴（六元组键）；
4. 给这些元素打一记「刚被提到」的增益，之后随对话慢慢回落。

不再走「旧库写摘要 → sync 二次加工」那条路：同一件事被加工两次，中间会掉东西。
"""
from __future__ import annotations

import sqlite3

from . import activation, db, extract, normalize as nz, rewrite, timeline
from .lexicon import AFFECT
from .canon import canon

MAX_ELEMENTS = 14      # 写入时留得比检索宽得多


def _is_done(con: sqlite3.Connection, src_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM mem_src WHERE src_id=? AND fp_ver LIKE 'write-%' LIMIT 1",
        (src_id,)).fetchone() is not None


def _key_from(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT MAX(order_key) k FROM mem_src WHERE order_key LIKE '3:%'").fetchone()
    base = str(row["k"]) if row and row["k"] else None
    if not base:
        return timeline._key_text((timeline.NOW, 0, 0, 20261118, 0, 1))
    parts = base.split(":")
    parts[5] = f"{int(parts[5]) + 1:06d}"
    return ":".join(parts)


def process_tab(con: sqlite3.Connection, tab: sqlite3.Row, verbose: bool = False) -> dict | None:
    """把一条 tab 变成元图里的一条记忆。"""
    from . import extract as _ex
    _ex.load_stop_from_rules(con)          # 抽取口径唯一：停用表在图里
    src_id = str(tab["id"])
    if _is_done(con, src_id):
        return None

    body = nz.norm(" ".join(x for x in (tab["title"], tab["summary"]) if x))
    if not body:
        return None

    fp, ver = body, "write-rules"
    try:
        got = rewrite.rewrite(str(tab["title"] or ""), body, str(tab["event_time"] or ""))
        fp, ver = got["memory"], f"write-{got['ver']}"
    except Exception:
        pass

    mem = db.node(con, "mem", str(tab["title"] or src_id)[:40], uniq=f"mem|{src_id}")
    key = timeline.order_key(tab["arc"], tab["event_id"], tab["event_time"],
                             tab["source_refs_json"], src_id)
    con.execute(
        "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
        "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
        (mem, src_id, body, fp, ver, str(tab["event_time"] or ""),
         timeline._key_text(key), "archive"))

    # 全过程抽取：元素一次给够，后面才谈得上挑
    try:
        _, items = extract.run(fp, "llm")
    except Exception:
        _, items = extract.run(fp, "rules")
    n_el = 0
    touched: list[int] = []
    for it in items[:MAX_ELEMENTS]:
        surface = canon(str(it.get("surface", "")))
        if not surface:
            continue
        kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
        el = db.node(con, kind, surface, uniq=f"{kind}|{surface}")
        if el == mem:
            continue
        db.edge(con, mem, el, "has:lead" if it.get("role") == "lead" else "has")
        touched.append(el)
        n_el += 1
    con.commit()

    # 写进去的那一刻就归义项：图在长，义项跟着长，不留到以后再算
    try:
        from . import sense as sense_mod
        table = sense_mod.idf(con)
        for surface in {canon(str(it.get("surface", ""))) for it in items[:MAX_ELEMENTS]}:
            if surface:
                sense_mod.assign(con, surface, mem, table)
    except Exception:
        pass

    # 刚发生的，先热一记
    activation.bump(con, touched, amount=1.0)

    if verbose:
        print(f"  {src_id} → 记忆 {mem}，元素 {n_el} 个，{ver}")
    return {"mem": mem, "elements": n_el, "ver": ver}


def process_new(con: sqlite3.Connection, src_path: str, limit: int = 0,
                verbose: bool = True) -> dict:
    """把库里新出现的记忆逐条做完。"""
    con.row_factory = sqlite3.Row
    src = sqlite3.connect(f"file:{src_path.replace(chr(92), '/')}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT id, title, summary, event_id, event_time, source_refs_json, arc "
        "FROM tabs ORDER BY event_time, id").fetchall()
    src.close()

    done = skipped = 0
    for r in rows:
        if str(r["arc"] or "").startswith("旧版"):
            continue
        got = process_tab(con, r, verbose=verbose)
        if got:
            done += 1
            if limit and done >= limit:
                break
        else:
            skipped += 1
    if verbose:
        print(f"新记忆处理完：{done} 条（已处理过 {skipped} 条）")
    return {"done": done, "skipped": skipped}
