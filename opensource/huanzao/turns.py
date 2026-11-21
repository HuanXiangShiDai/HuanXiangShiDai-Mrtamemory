"""把对话上下文也收进图里：原样存，再抽元素。

和 `replay` 的折块不同——折块是「压成一条自传体记忆」，只留他说的话；
这里要的是**原文**：user 说了什么、assistant 回了什么，一字不改地留着，
然后再从这段原文里抽元素。

两种原文来源：
- `evicted` / `current_turn`：运行时的当下层，出窗了就该进图（不是丢掉）；
- `library`：真库 `turns` 表里的历史对话（七千多轮，默认限量）。
"""
from __future__ import annotations

import sqlite3

from . import activation, db, extract, timeline
from .canon import canon
from .lexicon import AFFECT

MAX_ELEMENTS = 14


def _next_key(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT MAX(order_key) k FROM mem_src WHERE order_key LIKE '3:%'").fetchone()
    base = str(row["k"]) if row and row["k"] else None
    if not base:
        return timeline._key_text((timeline.NOW, 0, 0, 20261119, 0, 1))
    parts = base.split(":")
    parts[5] = f"{int(parts[5]) + 1:06d}"
    return ":".join(parts)


def _strip_own(text: str) -> str:
    """把自己塞进去的脚手架切掉——那是我们的东西，不是他说的话。

    认三种：
    - 【此刻浮上来的】（提醒块）
    - 〔记忆注入｜脚手架…〕…〔／记忆注入〕（写进 messages 的记忆块）
    - 「不必逐字复述…」老措辞

    这一步是**闸门**：回放折块只折「他说的话」，如果注入块没被切掉，
    她就会把「他跟我说：〔记忆注入〕…」折进自己的记忆——自己造一条假记忆。
    """
    if "〔记忆注入" in text:
        head = text.find("〔记忆注入")
        tail = text.find("〔／记忆注入〕")
        if tail >= 0:
            text = (text[:head] + text[tail + len("〔／记忆注入〕"):]).strip()
        else:
            text = text[:head].strip()
    i = text.find("【此刻浮上来的】")
    if i < 0:
        return text.strip()
    tail = text[i:]
    cut = len(tail)
    for mark in ("（这些是你心里浮上来的", "不必逐字复述"):
        j = tail.find(mark)
        if j >= 0:
            k = tail.find("\n", j)
            cut = min(cut, (k + 1) if k >= 0 else len(tail))
    return (text[:i] + tail[cut:]).strip()


def _pairs(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """把消息流配成「他一说、我一答」。落单的也留着。"""
    out: list[tuple[str, str]] = []
    pending = ""
    for role, text in rows:
        if role == "user":
            if pending:
                out.append((pending, ""))
            pending = _strip_own(text)
        else:
            out.append((pending, text.strip()))
            pending = ""
    if pending:
        out.append((pending, ""))
    return out


def ingest(con: sqlite3.Connection, source: str = "evicted", limit: int = 20,
           verbose: bool = True, consume: bool = True) -> dict:
    """把对话原文收进图。consume=False 时不动源表——回放要先存档、再折块，
    两步都得看见同一批出窗的对话。"""
    con.row_factory = sqlite3.Row
    # 元素选取的停用表在图里（rule|元素选取）。抽之前读一次——
    # 这样「哪些片段不成为元素」就由图决定，而不是由代码常量决定。
    try:
        extract.load_stop_from_rules(con)
    except Exception:
        pass
    if source in ("evicted", "current"):
        # `current` 是个别名，真实表名是 `current_turn`。
        # 早先直接拼成 FROM current，运行时才炸 no such table（实测踩到）。
        table = "current_turn" if source == "current" else "evicted"
        rows = con.execute(
            f"SELECT id, role, content FROM {table} ORDER BY id LIMIT ?", (limit,)).fetchall()
        seq = [(("user" if r["role"] == "user" else "assistant"), r["content"]) for r in rows]
    elif source == "library":
        from .sync import SRC_DEFAULT
        src = sqlite3.connect(f"file:{SRC_DEFAULT.replace(chr(92), '/')}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        seq = [((("user" if r["role"] == "owner" else "assistant")), r["text"])
               for r in src.execute("SELECT role, text FROM turns ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()]
        seq.reverse()
        src.close()
    else:
        raise SystemExit("source 只能是 evicted / current / library")

    pairs = _pairs(seq)
    added, elements_total = 0, 0
    for user_text, her_text in pairs:
        if not (user_text or her_text):
            continue
        raw = f"他：{user_text}\n我：{her_text}".strip()
        uniq = f"mem|turn|{source}|{abs(hash(raw)) % 10**10}"
        mem = db.node(con, "mem", "对话：" + (user_text or her_text)[:20], uniq=uniq)
        con.execute(
            "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
            "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
            (mem, uniq.split("|", 2)[-1], raw, raw, "verbatim", "live",
             _next_key(con), f"turn-{source}"))

        try:
            _, items = extract.run(raw, "llm")
        except Exception:
            _, items = extract.run(raw, "rules")
        touched = []
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
        con.commit()
        activation.bump(con, touched)
        added += 1
        elements_total += len(touched)

    if source in ("evicted", "current") and added and consume:
        # 同上：`current` 是别名，表名是 `current_turn`。
        con.execute(f"DELETE FROM {'current_turn' if source == 'current' else 'evicted'}")
        con.commit()

    if verbose:
        print(f"收进来 {added} 轮对话（原文照留），元素 {elements_total} 个")
    return {"turns": added, "elements": elements_total}

def record(con: sqlite3.Connection, user_text: str, her_text: str) -> int:
    """把一轮真实对话登记进 `current_turn`，等 `ingest` 来收。

    这是**管道**：插件在每轮结束时调它，于是对话不再需要人手补。
    为什么先落表再 ingest、而不是直接写记忆：
    这样 `turns.ingest` 只需要面对一种输入（表里的 role/content），
    不用为"来自会话"和"来自出窗"写两套逻辑。
    """
    con.row_factory = sqlite3.Row
    n = 0
    for role, content in (("user", user_text), ("assistant", her_text)):
        content = (content or "").strip()
        if not content:
            continue
        con.execute(
            "INSERT INTO current_turn(role, content, tokens) VALUES(?,?,?)",
            (role, content, len(content)))
        n += 1
    con.commit()
    return n
