"""把上一代记忆库接进图里。**已于 2026-11-21 封存，默认拒绝执行。**

`chat_memory.db` 是上一代记忆系统（检索型：tabs + FTS + 带权重的边 + 情绪/驱力），
`graph.db` 是元图。两条并行的召回路径会让同一句话得到两个答案，所以决定只留元图。

现在源库在：
    `最终交付_幻造记忆层\\数据\\_封存_20261121\\chat_memory.db`

`sync()` 仍然可用（逻辑保留，将来要迁移随时能跑），但**必须显式 force=True**，
免得哪天一条命令把封存的东西又悄悄接回图里。

幂等：已经在图里的（`mem_src.src_id` 有记录）不会重复进来。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, normalize as nz, timeline

ARCHIVE_DIR = r"C:\Users\Administrator\Desktop\最终交付_幻造记忆层\数据\_封存_20261121"
SRC_DEFAULT = ARCHIVE_DIR + r"\chat_memory.db"
SRC_ARCHIVED = True          # 旧库已封存：默认路径就是封存路径，且 sync 拒绝执行


def _src_rows(src_path: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{Path(src_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, title, summary, event_id, event_time, source_refs_json, arc "
        "FROM tabs ORDER BY event_time, id").fetchall()
    con.close()
    return rows


def sync(con: sqlite3.Connection, src_path: str = None, verbose: bool = True,
         force: bool = False) -> dict:
    """把封存的旧记忆库迁进图。

    `force=False` 时**直接拒绝**：旧库已于 2026-11-21 封存，
    留在封存目录里是为了可追溯，不是为了被悄悄接回来。
    """
    src_path = src_path or SRC_DEFAULT
    if SRC_ARCHIVED and not force:
        raise SystemExit(
            "旧记忆库已封存（2026-11-21），不再作为活的记忆源。\n"
            "  位置：%s\n"
            "  确实要迁移进主图，请显式传 force=True（或先解封）。\n"
            "  说明见同目录的 封存说明.md。" % src_path)
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
