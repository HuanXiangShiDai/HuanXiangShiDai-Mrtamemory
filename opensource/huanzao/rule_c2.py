"""C2：规则写入的类型化通道——思考只能"指"，不能"写"。

为什么需要它（v0.4 定理 13 B 款）：
只剥规则正文（C1）挡不住同义改写。"以后别超过五步" 一字不沾规则正文，
却足以让人自撰出 `max_hops: 5`。所以**承重的是 C2**：
    规则改写只经 (规则, 原子操作, 参数) 三元组、从既有规则节点导出；
    思考只输出"哪条规则、哪个操作、哪个参数"，**不生成规则正文**。

这道闸落在三处：

1. 操作白名单：只有六种原子操作（定义 8）。
2. 参数白名单：键只能是 key/to/a/b/w/into；值必须是短标量（≤40 字），
   含换行或分号的直接拒——那是正文的形状，不是参数。
3. 新正文只由 `rule_graph.apply_op` 从旧正文推导。思考这一侧永远拿不到正文句柄。

另外每次写入都记一条 `enacts` 边：**哪条总结促成了这次改写**。
这是 L3 的入口——没有它，δ* 恒等于 1。
"""
from __future__ import annotations

import sqlite3

from . import db, rule_graph

TYPED_OPS = set(rule_graph.OP_POLARITY)          # merge / stop+ / stop- / thr↑ / thr↓ / split
ALLOWED_ARGS = {"key", "to", "a", "b", "w", "into"}
MAX_ARG_CHARS = 40
ENACT_UP = "enacts_up"
ENACT_DOWN = "enacts_down"


class C2Error(ValueError):
    """一次不符合 C2 的写入尝试。"""


def _check_payload(payload: dict) -> None:
    for k, v in payload.items():
        if k not in ALLOWED_ARGS:
            raise C2Error("参数键 %r 不在白名单 %s 里" % (k, sorted(ALLOWED_ARGS)))
        if isinstance(v, (int, float, bool)):
            continue
        s = str(v)
        if len(s) > MAX_ARG_CHARS:
            raise C2Error("参数 %s 的值太长（%d 字 > %d）——C2 不许携带正文"
                          % (k, len(s), MAX_ARG_CHARS))
        if "\n" in s or "；" in s or "：" in s:
            raise C2Error("参数 %s 的值像正文（含换行/分号/冒号），C2 拒绝" % k)


def typed_write(con: sqlite3.Connection, summary: str, rule_name: str, op: str,
                payload: dict | None = None, by: str = "thought", why: str = "") -> dict:
    """思考改规则的唯一入口。summary 是促成这次改写的总结（sum|session|turn|step）。"""
    op_c = rule_graph._canon_op(op)              # 白名单外的操作在这里就抛
    if op_c not in TYPED_OPS:
        raise C2Error("C2 只接受原子操作，收到 %r" % op_c)
    payload = dict(payload or {})
    _check_payload(payload)
    # `rule_graph._as_number` 只吃字符串（传 int 会 TypeError）——这是它的实现缺陷，
    # 闸门这边统一转成 str，不去动它的代码。
    payload = {k: (str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
               for k, v in payload.items()}

    s = db.get_node(con, summary)
    if not s:
        raise C2Error("找不到总结：%s" % summary)

    res = rule_graph.apply_op(con, rule_name, op_c, payload, by=by,
                              why=why or ("由 %s 促成的原子改写" % summary))

    # 版本节点命名是 rule|<name>|v<n>（带 v）
    name = rule_graph._normalize_name(rule_name)
    ver = db.get_node(con, "rule|%s|%s" % (name, res["version"]))
    vid = int(ver["id"]) if ver else (int(res["id"]) if res.get("id") else None)
    rel = None
    if vid:
        rel = ENACT_UP if int(res.get("polarity") or 0) > 0 else ENACT_DOWN
        db.edge(con, int(s["id"]), vid, rel)
        # 闭合边：如果这一版回到**某个更早版本**的状态（键值全同），这就是一圈的终点。
        # 否定之否定 = 数值回到原点，但版本链多出两层；没有这条 closes 边，环检测看不见它。
        closed = _close_to_earlier(con, name, vid)
        con.commit()
    else:
        closed = None
    res.update({"summary": summary, "enacts": rel, "closes": closed})
    return res


def _close_to_earlier(con: sqlite3.Connection, name: str, vid: int) -> str | None:
    """这一版是否回到了某个更早版本的状态？回到就建 closes 边，返回那个版本号。"""
    row = con.execute("SELECT body FROM nodes WHERE id=?", (vid,)).fetchone()
    if not row:
        return None
    now = rule_graph.parse_body(str(row["body"]))
    for r in con.execute(
            "SELECT id, uniq, body FROM nodes WHERE kind='rule' AND uniq LIKE ? "
            "AND id <> ? ORDER BY id", ("rule|%s|v%%" % name, vid)).fetchall():
        if rule_graph.parse_body(str(r["body"])) == now:
            db.edge(con, vid, int(r["id"]), "closes")
            return str(r["uniq"]).split("|")[-1]
    return None


def depth(con: sqlite3.Connection) -> dict:
    """δ*：同一维度被**连续**改写了多少次。

    判据不是"改了三次"，而是"改成 v2 之后又被改成 v3，而且两次都有总结促成"——
    这就是否定之否定的结构：v1 →(总结A) v2 →(总结B) v3。
    """
    per_rule: dict[str, int] = {}
    for r in con.execute(
            "SELECT s.uniq AS sum_uniq, v.uniq AS ver_uniq FROM edges e "
            "JOIN nodes s ON s.id=e.src JOIN nodes v ON v.id=e.dst "
            "WHERE e.rel IN ('enacts_up','enacts_down')").fetchall():
        name = str(r["ver_uniq"]).split("|")[1] if "|" in str(r["ver_uniq"]) else "?"
        per_rule[name] = per_rule.get(name, 0) + 1
    star = max(per_rule.values(), default=0)
    return {"δ*": star, "per_rule": per_rule, "L": _level(star)}


def _level(star: int) -> str:
    if star >= 3:
        return "L4"
    if star >= 2:
        return "L3"
    if star >= 1:
        return "L1/L2"
    return "L0"


def audit_c2(con: sqlite3.Connection) -> dict:
    """查有没有人绕过 C2 写规则。

    C2 的形状是：非人工写入必须走 typed_write（原子操作 + 白名单参数），
    出来必带一条 enacts 边。所以判据很简单——
    **有一个版本节点既不是 v1 基线、又没有任何 enacts 入边，说明它是被自由文本写进来的。**

    人工改写（by=lead/tester/system/admin）豁免：那是人，不是思考。
    """
    humans = {"lead", "tester", "system", "admin"}
    acted = {int(r["dst"]) for r in con.execute(
        "SELECT dst FROM edges WHERE rel IN ('enacts_up','enacts_down')")}
    bad = []
    for r in con.execute(
            "SELECT id, uniq FROM nodes WHERE kind='rule' AND uniq LIKE 'rule|%|v%'").fetchall():
        nid, uniq = int(r["id"]), str(r["uniq"])
        if uniq.endswith("v1") or nid in acted:
            continue
        name = uniq.split("|")[1]
        ver = uniq.split("|")[2]
        info = {}
        try:
            info = rule_graph._log_info(con, name, ver)
        except Exception:
            pass
        by = str(info.get("by") or "")
        if by and by not in humans:
            bad.append({"rule": uniq, "by": by, "op": info.get("op")})
    return {"ok": not bad, "free_text_writes": bad,
            "note": "非人工写入必须走 typed_write，出来必带 enacts 边"} 
