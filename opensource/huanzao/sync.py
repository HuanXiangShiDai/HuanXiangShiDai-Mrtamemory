"""把活的记忆库接进图里。

`chat_memory.db` 是记忆的源头（她平时读写的那个），`graph.db` 是元图。
这个模块做的是单向同步：

    新出现的记忆（tab） → 建点 → 写原文与第一人称 → 排叙事位置

幂等：已经在图里的（`mem_src.src_id` 有记录）不会重复进来。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, normalize as nz, timeline

SRC_DEFAULT = r"C:\Users\Administrator\Desktop\最终交付_幻造记忆层\数据\chat_memory.db"


def _src_rows(src_path: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{Path(src_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, title, summary, event_id, event_time, source_refs_json, arc "
        "FROM tabs ORDER BY event_time, id").fetchall()
    con.close()
    return rows


def sync(con: sqlite3.Connection, src_path: str = SRC_DEFAULT, verbose: bool = True) -> dict:
    rows = _src_rows(src_path)
    have = {str(r["src_id"]) for r in con.execute("SELECT src_id FROM mem_src").fetchall()}
    new = [r for r in rows if str(r["id"]) not in have]
    retired = [r for r in new if str(r["arc"] or "").startswith("旧版")]
    new = [r for r in new if r not in retired]

    added = 0
    for r in new:
        when = str(r["event_time"] or "")
        body = nz.norm(" ".join(x for x in (r["title"], r["summary"]) if x))
        if not body:
            continue
        mem = db.node(con, "mem", str(r["title"] or r["id"])[:40], uniq=f"mem|{r['id']}")
        key = timeline.order_key(r["arc"], r["event_id"], when,
                                 r["source_refs_json"], r["id"])
        con.execute(
            "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
            "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
            (mem, r["id"], body, nz.first_person_rules(body), "rules-v0", when,
             timeline._key_text(key), "chat_memory"))
        added += 1
    con.commit()

    if verbose:
        print(f"源库 {len(rows)} 条，图里原有 {len(have)} 条，新接进来 {added} 条"
              + (f"（跳过退役 {len(retired)} 条）" if retired else ""))
    return {"added": added, "skipped_retired": len(retired), "source_total": len(rows)}


def pending(con: sqlite3.Connection, src_path: str = SRC_DEFAULT) -> dict:
    """还没抽元素的记忆有多少——接进来只是建了壳，元素要另跑抽取。"""
    row = con.execute(
        "SELECT COUNT(*) c FROM mem_src s WHERE s.when_iso <> 'canon' AND NOT EXISTS "
        "(SELECT 1 FROM extract_run r WHERE r.mem_node = s.mem_node AND r.mode='llm')"
    ).fetchone()
    return {"need_extract": int(row["c"])}
