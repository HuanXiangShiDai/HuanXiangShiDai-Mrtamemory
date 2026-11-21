"""思考进图：**边想边写**，不是事后补录。

为什么必须在过程中：thinking 不进召回，所以它「早进图」和「晚进图」对下一轮读到什么
没有区别——除了两件事，而这两件事只有在过程中做才有意义：

1. **推动脉冲**：思考里提到的元素立刻 `activation.bump()`，下一步的种子排序就变了。
   事后补录的话，那一刻已经过去了，补上去只是记账。
2. **留下痕迹**：`thought --thought-of--> mem` 记下「想出这段时，它想到了哪几条记忆」。
   事后补录也能记，但那时它已经不知道当时在想什么了。

落法（靠表的口径隔离，不靠到处加 where）：

    node  kind='thought'
    edge  thought --thought-of--> mem     只有这一种出边；**绝不建 has**
    edge  thought --next--> thought       同一会话按 (turn, step, seq) 串链
    不写 mem_src ⇒ 自传、时间轴、召回、头段、骨干、回放，全都天然看不见它

保留策略：每个会话最多留 `keep` 条独立 thought，更早的折成**一条汇总**
（「那段时间我反复在想：…」），原条目不删——只增不改。汇总条挂 `supersedes`。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import activation, db, retrieve

BODY_CAP = 4000
DEFAULT_KEEP = 200


# ── 从会话日志里取思考（DSH 的 session.v4.jsonl.zstd） ──────────────────
def parse_session(path: Path) -> list[dict]:
    from compression import zstd          # Python 3.14 自带
    sid = path.parent.name.replace("session-", "")
    try:
        raw = zstd.decompress(path.read_bytes()).decode("utf-8", "replace")
    except Exception:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant/message":
            continue
        data = rec.get("data") or {}
        msg = data.get("message") or {}
        text = "\n".join(str(b.get("text") or "") for b in (msg.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "reasoning").strip()
        if not text:
            continue
        stamp = ""
        try:
            stamp = datetime.fromtimestamp(int(rec["time"]) / 1000).isoformat(timespec="seconds")
        except Exception:
            pass
        out.append({"session": sid, "turn": data.get("turn") or 0,
                    "step": data.get("step") or 0, "seq": 0, "time": stamp, "text": text})
    return out


# ── 写入 ──────────────────────────────────────────────────────────────
def _elements_in(con: sqlite3.Connection, text: str) -> list[int]:
    """思考里提到了哪些元素。

    **必须过滤虚词**：思考是连续的散文/代码混排，不过滤的话「一半」「一句」
    「一次」「上次」这种碎片会被当成元素抬进激活层——脉冲 top10 立刻全是废词
    （2026-11-20 实测踩到）。所以和 `causal._hot` 用同一张跳过表。
    """
    try:
        from . import rules as rules_mod
        skip = rules_mod.op_words(con) | rules_mod.topic_words(con) | retrieve.NOISE | retrieve.META
    except Exception:
        skip = retrieve.NOISE | retrieve.META
    rows = con.execute(
        "SELECT n.id, n.body FROM nodes n LEFT JOIN derived_df d ON d.node_id = n.id "
        "WHERE n.kind IN ('entity','affect','person') AND LENGTH(n.body) >= 2").fetchall()
    out = []
    for r in rows:
        body = str(r["body"])
        if body in skip:
            continue
        # df=0 是孤儿元素：没有任何记忆引用它，抬了它等于抬空气。
        # 「一半」「一句」「一次」全是这一类（实测 df=0），不筛掉脉冲 top10 就被它们占满。
        row = con.execute("SELECT df FROM derived_df WHERE node_id=?", (int(r["id"]),)).fetchone()
        if not row or int(row["df"]) == 0:
            continue
        if body in text:
            out.append(int(r["id"]))
    return out


def _mems_of(con: sqlite3.Connection, elem_ids: list[int], k: int = 2) -> list[int]:
    if not elem_ids:
        return []
    marks = ",".join(str(i) for i in elem_ids)
    rows = con.execute(
        f"SELECT src AS mem, COUNT(*) c FROM edges WHERE rel IN ('has','has:lead') "
        f"AND dst IN ({marks}) GROUP BY src ORDER BY c DESC, mem LIMIT ?", (k,)).fetchall()
    return [int(r["mem"]) for r in rows]


def record(con: sqlite3.Connection, t: dict, bump: bool = True) -> int:
    """写一条思考。返回节点 id。

    `bump=True` 时把命中的元素抬一记——这是「实时」唯一真正有用的后果。
    """
    uniq = f"thought|{t['session']}|{t['turn']}|{t['step']}|{t.get('seq', 0)}"
    text = str(t["text"])
    node = db.node(con, "thought", text[:BODY_CAP], uniq=uniq)
    con.execute("UPDATE nodes SET body=? WHERE id=?", (text[:BODY_CAP], node))

    elems = _elements_in(con, text)
    for mem in _mems_of(con, elems):
        db.edge(con, node, mem, "thought-of")
    # 串链：同一会话里按 (turn, step, seq) 接上一条
    prev = con.execute(
        "SELECT n.id FROM nodes n WHERE n.kind='thought' AND n.uniq LIKE ? "
        "AND n.id <> ? ORDER BY n.id DESC LIMIT 1",
        (f"thought|{t['session']}|%", node)).fetchone()
    if prev:
        db.edge(con, int(prev["id"]), node, "next")
    con.commit()

    if bump and elems:
        # 轻推，不是灌顶：一条思考只抬它**最锐的三个元素**，而且幅度减半。
        # 原来把 14 个元素全按 BOOST 抬，92 条思考之后全员封顶 3.0，
        # 脉冲 top10 就成了一张人名表，失去分辨力（2026-11-20 实测踩到）。
        try:
            total = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"] or 1
            dfs = {int(r["node_id"]): int(r["df"]) for r in con.execute(
                "SELECT node_id, df FROM derived_df")}
            ranked = sorted(elems, key=lambda e: dfs.get(e, 0))[:3]   # df 小的更锐
            activation.bump(con, ranked, amount=activation.BOOST * 0.5)
        except Exception:
            pass
    return node


# ── 保留策略 ──────────────────────────────────────────────────────────
def prune(con: sqlite3.Connection, keep: int = DEFAULT_KEEP, apply: bool = False) -> dict:
    """每个会话只留最近 keep 条独立 thought，更早的折成一条汇总（原条目不删）。"""
    sessions = [r["s"] for r in con.execute(
        "SELECT DISTINCT substr(uniq, 10, instr(substr(uniq, 10), '|') - 1) s "
        "FROM nodes WHERE kind='thought'")]
    folded, kept = 0, 0
    for sid in sessions:
        rows = con.execute(
            "SELECT id, body FROM nodes WHERE kind='thought' AND uniq LIKE ? ORDER BY id",
            (f"thought|{sid}|%",)).fetchall()
        if len(rows) <= keep:
            kept += len(rows)
            continue
        old, new = rows[:len(rows) - keep], rows[len(rows) - keep:]
        keys = []
        for r in old:
            for e in _elements_in(con, str(r["body"]))[:4]:
                row = con.execute("SELECT body FROM nodes WHERE id=?", (e,)).fetchone()
                if row and str(row["body"]) not in keys:
                    keys.append(str(row["body"]))
        summary = f"那段时间我反复在想：{'、'.join(keys[:6]) or '一些说不清的事'}。"
        if apply:
            agg = db.node(con, "thought", summary, uniq=f"thought|{sid}|folded")
            for r in old:
                db.edge(con, agg, int(r["id"]), "supersedes")
            con.commit()
        folded += len(old)
        kept += len(new)
    return {"sessions": len(sessions), "kept": kept, "folded": folded}


def stats(con: sqlite3.Connection) -> dict:
    return {
        "thoughts": con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='thought'").fetchone()["c"],
        "chars": con.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0) c FROM nodes WHERE kind='thought'").fetchone()["c"],
        "links": con.execute("SELECT COUNT(*) c FROM edges WHERE rel='thought-of'").fetchone()["c"],
        "chains": con.execute(
            "SELECT COUNT(*) c FROM edges WHERE rel='next' AND src IN "
            "(SELECT id FROM nodes WHERE kind='thought')").fetchone()["c"],
    }
