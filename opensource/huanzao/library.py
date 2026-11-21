"""往活的记忆库写一条。

她伸手记下的东西，先落进 `chat_memory.db`（那是真库，别处也读得到），
再当场进元图——不是先写摘要、以后再加工。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

OWNER = "p_owner"
SESSION = "s_elysia_live"


def _next_epoch(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT MAX(epoch) e FROM tabs").fetchone()
    return int(row["e"] or 0) + 1


def add_tab(src_path: str, title: str, summary: str, when: str = "live",
            w0: float = 0.4, lam: float = 0.06) -> dict:
    con = sqlite3.connect(src_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    digest = hashlib.md5((title + summary).encode("utf-8")).hexdigest()[:10]
    tab_id = f"tab_elysia_{digest}"
    have = con.execute("SELECT id FROM tabs WHERE id=?", (tab_id,)).fetchone()
    if have:
        con.close()
        return {"id": tab_id, "created": False}

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO tabs(id, owner_id, session_id, title, summary, entities_json, "
        "event_time, epoch, source_refs_json, w0, lambda, layer, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tab_id, OWNER, SESSION, title, summary, "[]", when, _next_epoch(con),
         "[]", w0, lam, "retrievable", "active", now, now))
    con.commit()
    row = con.execute("SELECT id, title, summary, event_id, event_time, "
                      "source_refs_json, arc FROM tabs WHERE id=?", (tab_id,)).fetchone()
    con.close()
    return {"id": tab_id, "created": True, "row": dict(row)}
