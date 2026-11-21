"""元图的读写：只建点、只连边，不做属性表。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def init(path: Path | str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    return con


def node(con: sqlite3.Connection, kind: str, body: str, uniq: str | None = None) -> int:
    """取或建一个点。uniq 是它的身份，重复调用不会长出第二个。"""
    key = uniq if uniq is not None else f"{kind}|{body}"
    row = con.execute("SELECT id FROM nodes WHERE uniq = ?", (key,)).fetchone()
    if row:
        return int(row["id"])
    cur = con.execute("INSERT INTO nodes(kind, body, uniq) VALUES(?,?,?)", (kind, body, key))
    return int(cur.lastrowid)


def edge(con: sqlite3.Connection, src: int, dst: int, rel: str) -> None:
    con.execute("INSERT OR IGNORE INTO edges(src, dst, rel) VALUES(?,?,?)", (src, dst, rel))


def get_node(con: sqlite3.Connection, uniq: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM nodes WHERE uniq = ?", (uniq,)).fetchone()


def set_meta(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def get_meta(con: sqlite3.Connection, k: str, default: str = "") -> str:
    row = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return str(row["v"]) if row else default


def counts(con: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for kind in ("mem", "entity", "affect", "pred", "modal", "motif", "combo", "sense"):
        out[kind] = int(con.execute("SELECT COUNT(*) c FROM nodes WHERE kind=?", (kind,)).fetchone()["c"])
    out["edges"] = int(con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"])
    return out
