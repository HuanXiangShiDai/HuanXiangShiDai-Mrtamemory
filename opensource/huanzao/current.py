"""当下层：还热着的那几轮。

窗口有上限——轮数和 token 双重封顶。挤出去的对话不是消失，是「出窗」，
写进封存候选，等回放时压成块。

裁剪只在越过高水位时做一次：每轮都裁，前缀每轮都变，缓存等于没有。
"""
from __future__ import annotations

import sqlite3

MAX_TURNS = 8
MAX_TOKENS = 80       # 裁到这个水位以下
HIGH_WATER = 160      # 超过这个才动手裁


def ensure_table(con: sqlite3.Connection) -> None:
    con.executescript(
        "CREATE TABLE IF NOT EXISTS current_turn ("
        "  id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,"
        "  tokens INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL DEFAULT (datetime('now','localtime')));"
        "CREATE TABLE IF NOT EXISTS evicted ("
        "  id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,"
        "  tokens INTEGER NOT NULL DEFAULT 0, evicted_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));")
    con.commit()


def est(text: str) -> int:
    return int(len(text) / 1.5)


def append(con: sqlite3.Connection, role: str, content: str) -> None:
    con.execute("INSERT INTO current_turn(role, content, tokens) VALUES(?,?,?)",
                (role, content, est(content)))
    con.commit()


def turns(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT id, role, content, tokens FROM current_turn ORDER BY id").fetchall()


def trim(con: sqlite3.Connection, max_turns: int = MAX_TURNS,
         max_tokens: int = MAX_TOKENS) -> int:
    """挤出去：先按轮数，再按 token。出窗的进封存候选。"""
    rows = turns(con)
    keep = rows[-max_turns:] if max_turns > 0 else []
    budget, kept = 0, []
    for r in reversed(keep):
        if budget + r["tokens"] > max_tokens and kept:
            break
        budget += r["tokens"]
        kept.append(r)
    kept_ids = {r["id"] for r in kept}
    out = [r for r in rows if r["id"] not in kept_ids]
    for r in out:
        con.execute("INSERT INTO evicted(role, content, tokens) VALUES(?,?,?)",
                    (r["role"], r["content"], r["tokens"]))
        con.execute("DELETE FROM current_turn WHERE id=?", (r["id"],))
    con.commit()
    return len(out)


def window_tokens(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COALESCE(SUM(tokens),0) t FROM current_turn").fetchone()
    return int(row["t"])


def messages(con: sqlite3.Connection) -> list[dict]:
    return [{"role": r["role"], "content": r["content"]} for r in turns(con)]
