"""人格修正：从「一次思考」到「长期改变」。

为什么要单独一层：
- 单次思考不该改人格。否则一阵情绪就把她改坏了。
- 但**同一个方向被反复指向**，就该改——这才是「长期化」。

三类节点各司其职：

    sum（总结）  --revises-->  canon 卡       提案：我对这一条的看法变了
    sum          --mentions--> element        总结也挂元素（可被元素调用）
    canon 新卡   --supersedes--> canon 旧卡   生效：旧卡不再进头段

判定（净方向累积）：
    净票 = (#赞成 - #反对)
    当 |净票| >= threshold（默认 3）且 反对票 < 赞成票 时，才生成新卡。
    没到阈值就只是提案，躺在图里，不动人格。

只增不改：旧卡永不删，只是被 supersedes；头段跳过被取代的卡。
"""
from __future__ import annotations

import sqlite3

from . import db

DEFAULT_THRESHOLD = 3
DIRECT = {"+1": 1, 1: 1, "加强": 1, "变强": 1, "更": 1,
          "-1": -1, "-1": -1, "削弱": -1, "变弱": -1, "不再": -1}


def _dir(v) -> int:
    if isinstance(v, int):
        return 1 if v > 0 else (-1 if v < 0 else 0)
    return DIRECT.get(str(v).strip(), 0)


def propose(con: sqlite3.Connection, case_id: str, canon_uniq: str,
            direction, why: str = "") -> dict:
    """一条总结提出：我对某张 canon 卡的看法，往某个方向变了。"""
    from . import scratch
    sid, turn, step = scratch._split_case(case_id)
    sum_uniq = f"sum|{sid}|{turn}|{step}"
    s = db.get_node(con, sum_uniq)
    card = db.get_node(con, canon_uniq)
    if not s or not card:
        raise ValueError("找不到总结或 canon 卡：%s / %s" % (sum_uniq, canon_uniq))
    d = _dir(direction)
    if d == 0:
        raise ValueError("方向只能是 +1/-1（加强/削弱）")
    # 极性用谓词区分，不往边里塞属性。方向：总结 → 卡（tally 按 dst=卡 统计）
    rel = "revises_up" if d > 0 else "revises_down"
    db.edge(con, int(s["id"]), int(card["id"]), rel)
    db.edge(con, int(s["id"]), int(card["id"]), "revises")
    con.commit()
    return {"summary": sum_uniq, "canon": canon_uniq, "dir": d, "why": why}


def tally(con: sqlite3.Connection, canon_uniq: str) -> dict:
    """这张卡被多少总结指向、净方向多少。"""
    card = db.get_node(con, canon_uniq)
    if not card:
        raise ValueError("找不到 canon 卡：%s" % canon_uniq)
    up = con.execute("SELECT COUNT(*) c FROM edges WHERE dst=? AND rel='revises_up'",
                     (int(card["id"]),)).fetchone()["c"]
    down = con.execute("SELECT COUNT(*) c FROM edges WHERE dst=? AND rel='revises_down'",
                       (int(card["id"]),)).fetchone()["c"]
    return {"canon": canon_uniq, "up": up, "down": down, "net": up - down}


def apply(con: sqlite3.Connection, canon_uniq: str, new_body: str,
          threshold: int = DEFAULT_THRESHOLD, by: str = "system") -> dict:
    """净票到阈值才真的改人格：写新卡 + supersedes + 挂 innate。

    没到阈值 = 什么都不做（只回报票数），这样人格不会被一两次波动带走。
    """
    t = tally(con, canon_uniq)
    if abs(t["net"]) < threshold or t["net"] <= 0:
        return {"applied": False, "tally": t, "why": "净票未到阈值（或不为正）"}

    old = db.get_node(con, canon_uniq)
    n = con.execute("SELECT COUNT(*) c FROM nodes WHERE uniq LIKE ?",
                    (canon_uniq + "|r%",)).fetchone()["c"] + 1
    new_uniq = "%s|r%d" % (canon_uniq, n)
    new_id = db.node(con, "mem", new_body[:400], uniq=new_uniq)
    con.execute(
        "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
        "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
        (new_id, new_uniq, new_body, new_body, "persona", "canon",
         "0:000:0000000000:00000000:0000000000:%06d" % (900 + n), "canon:修订"))
    db.edge(con, new_id, int(old["id"]), "supersedes")
    center = con.execute("SELECT id FROM nodes WHERE kind='center' LIMIT 1").fetchone()
    if center:
        db.edge(con, int(center["id"]), new_id, "innate")
    con.commit()
    return {"applied": True, "tally": t, "new": new_uniq, "old": canon_uniq,
            "by": by, "body": new_body[:80]}


def revisions(con: sqlite3.Connection) -> list[dict]:
    """人格修订史：谁取代了谁。"""
    rows = con.execute(
        "SELECT a.body AS new_body, b.body AS old_body, a.uniq AS new_uniq "
        "FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
        "WHERE e.rel='supersedes'").fetchall()
    return [{"new": str(r["new_uniq"]), "now": str(r["new_body"])[:40],
             "was": str(r["old_body"])[:40]} for r in rows]
