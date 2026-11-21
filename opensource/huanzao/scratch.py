"""临时态：自指思考过程中的「工作台」，以及思考结束时压出来的那条总结。

为什么需要它
------------
自指思考（她判断「我现在在判什么、依据哪条规则」）的过程中，必须能随手记下
中间结论：这条是条件、这条是依据的规则名、这条是判别、这条是结论。
这些是**临时态**，不是记忆：

- 它们只在这一次思考还没结束时有效，思考一结束就没有回忆价值；
- 它们不进图。图里只有点跟边，是「她经历过的事」的真相；临时态一旦写进图，
  就会跟 mem 一样被召回、被排进时间轴、被折进头段——那等于把草稿当成往事。

所以：临时态挂在**一张独立的 `scratch` 表**里（`nodes` / `edges` 都不碰，
不写 `mem_src`，不建 `has` 边），图那边天然看不见它。

为什么这张表允许 DELETE
----------------------
它是全工程唯一允许删的地方。理由就一条：**临时态按 case 的生命周期失效**。
思考结束，这个 case 的临时态就没有任何保留意义；留着只会变成一张无人清理的
日志表。而图里的点跟边是「只增不改」的真相——那里绝不删。

关闭时做什么
------------
`close_case()` 把这个 case 的四类 note 压成**一条总结**写进图：

    node  kind='thought'
    uniq  sum|<session>|<turn>|<step>        # 本模块独有的前缀，不跟 thought.record 抢

总结用固定四段：在什么条件下 / 用了什么规则 / 对什么做了判别 / 得出什么结论。
**规则只写名字，不复制规则正文**——规则正文已经在 `kind='rule'` 的点里了，
再抄一份进总结就是同一段正文二次落库，以后改规则还会对不上。

写完总结，立刻 DELETE 这个 case 在 `scratch` 里的全部行。
重复 close 同一个 case 不会长出第二条总结（uniq 即身份，正文以最后一次为准）。

和 thought.py 的分工
--------------------
`thought.record()` 收的是**流式**思考（边想边写、带 thought-of 边、抬激活）；
这里收的是**压完的结论**（一条一个 case，不建边、不抬激活）。
两者都 kind='thought'，靠 uniq 前缀区分：`thought|…` vs `sum|…`。
"""
from __future__ import annotations

import sqlite3

from . import db

KINDS = ("condition", "rule", "judge", "conclusion")
BODY_CAP = 2000          # 单条 note 最多留多少字（临时态，不追求完整）
SECTION_CAP = 240        # 总结每段最多留多少字（总结是压缩，不是搬运）
NAME_CAP = 40            # 规则名最多多少个字

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scratch (
    id      INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    turn    INTEGER NOT NULL DEFAULT 0,
    step    INTEGER NOT NULL DEFAULT 0,
    kind    TEXT NOT NULL,          -- condition|rule|judge|conclusion
    body    TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_scratch_case ON scratch(session, turn, step);
"""


def ensure(con: sqlite3.Connection) -> None:
    """建临时态表。表只在这一个模块里，不写进 schema.sql（它不属于图）。"""
    con.executescript(TABLE_SQL)
    con.commit()


# ── case 的身份 ────────────────────────────────────────────────────────
def _case_id(session: str, turn: int, step: int) -> str:
    return f"{session}|{int(turn)}|{int(step)}"


def _split_case(case_id: str) -> tuple[str, int, int]:
    try:
        sid, turn, step = str(case_id).rsplit("|", 2)
        return sid, int(turn), int(step)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"不是合法的 case_id：{case_id!r}") from exc


def open_case(con: sqlite3.Connection, session: str, turn: int, step: int) -> str:
    """开一个思考 case，返回它的 id。

    **不插任何行**：case 的身份就是 (session, turn, step)，note 自己带这三列。
    临时态只有一张表，不为 case 单开表、也不为它单开一行——
    否则 close 时「关掉这个 case」就不再是一句 DELETE。
    """
    ensure(con)
    sid = str(session).strip() or "anonymous"
    return _case_id(sid, turn, step)


# ── 思考过程中随手记 ──────────────────────────────────────────────────
def note(con: sqlite3.Connection, case_id: str, kind: str, body: str) -> int:
    """记一条临时态。kind ∈ {condition, rule, judge, conclusion}。

    返回这行的 rowid。同一 case 可以写多条同类 note（比如两个条件）。
    """
    ensure(con)
    k = str(kind).strip()
    if k not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}，收到 {kind!r}")
    text = str(body).strip()
    if not text:
        raise ValueError("note 正文不能为空")
    sid, turn, step = _split_case(case_id)
    cur = con.execute(
        "INSERT INTO scratch(session, turn, step, kind, body) VALUES(?,?,?,?,?)",
        (sid, turn, step, k, text[:BODY_CAP]))
    con.commit()
    return int(cur.lastrowid)


# ── 关闭：压成一条总结 → DELETE 临时态 ───────────────────────────────
def _rule_name(raw: str) -> str:
    """从「规则身份」里只取出名字。

    规则正文在 `kind='rule'` 的点里，总结里再抄一遍就是二次落库；
    所以这里只留名字：`rule|中心校验|v3` → `中心校验`；
    就算调用方顺手贴了正文（带「：」或「（…）」），也只留名字那一段。
    """
    t = str(raw).strip()
    if t.startswith("rule|"):
        parts = t.split("|")
        t = parts[1] if len(parts) >= 2 else t
    for sep in ("：", ":"):
        if sep in t:
            t = t.split(sep)[0]
    for sep in ("（", "("):
        if sep in t:
            t = t.split(sep)[0]
    t = t.strip()
    return (t[:NAME_CAP] + "…") if len(t) > NAME_CAP else t


def rule_name(raw: str) -> str:
    """公开的规则名规范化（总结里只写名字，理由见 `_rule_name`）。"""
    return _rule_name(raw)


def _clip(text: str, cap: int = SECTION_CAP) -> str:
    text = " ".join(str(text).split())
    return (text[:cap] + "…") if len(text) > cap else text


def _summary(sid: str, turn: int, step: int, buckets: dict[str, list[str]]) -> str:
    def joined(kind: str, sep: str) -> str:
        vals = [(_rule_name(v) if kind == "rule" else _clip(v)) for v in buckets[kind]]
        return sep.join(v for v in vals if v) or "（没记）"

    return (
        f"自指思考总结（会话 {sid} 第 {turn} 轮第 {step} 步）\n"
        f"一、在什么条件下：{joined('condition', '；')}\n"
        f"二、用了什么规则：{joined('rule', '、')}\n"
        f"三、对什么做了判别：{joined('judge', '；')}\n"
        f"四、得出什么结论：{joined('conclusion', '；')}"
    )


def _elements_of(con: sqlite3.Connection, text: str) -> list[int]:
    """总结里提到的元素（与 thought 用同一套过滤：跳过虚词与 df=0 的孤儿）。"""
    try:
        from . import thought as thought_mod
        return thought_mod._elements_in(con, text)
    except Exception:
        return []


def close_case(con: sqlite3.Connection, case_id: str) -> dict:
    """把一个 case 收口：压成一条总结写进图，再删掉它的 scratch 行。

    返回 dict：case_id / session / turn / step / uniq / node / body /
    scratch_deleted / already_closed。

    已 close 过、又没有新 note 的 case 不会重复长总结：直接回报已有那条
    （already_closed=True）。没有 note 也从没 close 过的 case 报 ValueError。
    """
    ensure(con)
    sid, turn, step = _split_case(case_id)
    uniq = f"sum|{sid}|{turn}|{step}"

    rows = con.execute(
        "SELECT kind, body FROM scratch WHERE session=? AND turn=? AND step=? ORDER BY id",
        (sid, turn, step)).fetchall()

    if not rows:
        old = db.get_node(con, uniq)
        if old:
            return {"case_id": case_id, "session": sid, "turn": turn, "step": step,
                    "uniq": uniq, "node": int(old["id"]), "body": str(old["body"]),
                    "scratch_deleted": 0, "already_closed": True}
        raise ValueError(f"case 没有 note，也从未 close 过：{case_id!r}")

    buckets: dict[str, list[str]] = {k: [] for k in KINDS}
    for r in rows:
        buckets[str(r["kind"])].append(str(r["body"]))

    body = _summary(sid, turn, step, buckets)
    node = db.node(con, "thought", body, uniq=uniq)      # uniq 即身份：重复 close 也只有一条
    con.execute("UPDATE nodes SET body=? WHERE id=?", (body, node))

    # 总结不能是孤岛。它必须能回答"我是根据什么得出这个结论的"：
    #   sum --thought-of--> mem   它想到的那几条记忆
    #   sum --refers--> rule      它用到的那条规则（按名字解析到 current 版本）
    # 不连的话，总结在图里是个 0 边的点——既不可达，也无法回溯（2026-11-20 实测发现）。
    try:
        from . import thought as thought_mod
        elems = thought_mod._elements_in(con, body)
        for mem in thought_mod._mems_of(con, elems, k=3):
            db.edge(con, node, mem, "thought-of")
    except Exception:
        pass
    for name in buckets.get("rule", []):
        try:
            from . import rule_graph
            target = rule_graph._current_id(con, name) or \
                rule_graph._node(con, rule_graph._anchor_uniq(name))
            if target:
                db.edge(con, node, int(target["id"]), "refers")
        except Exception:
            continue
    # 总结也要挂元素：这样「元素」能反过来调用「我因为这件事得出的结论」。
    # 元素、记忆、规则、总结四者于是都通过 X 相连——没有谁是孤岛。
    for el in _elements_of(con, body):
        db.edge(con, node, el, "mentions")

    # 最后一步：把「判别 + 结论」写成一条**常规记忆**，并让它指向前提规则。
    # 条件段丢掉（只对当时那一刻有意义），原文留在 thought 节点里不动。
    # 不这么做的话，"思考得出过什么"在图上不存在——四段只是格式（实测）。
    try:
        from . import conclusion
        conclusion.ensure_rule(con)          # 处理规则也进图（rule|思考结论）
        got = conclusion.materialize(con, uniq, body)
    except Exception as e:
        got = {"made": False, "why": str(e)[:60]}
    con.commit()

    # 思考结束，临时态就该消失——这是全工程唯一允许删的地方。
    cur = con.execute(
        "DELETE FROM scratch WHERE session=? AND turn=? AND step=?", (sid, turn, step))
    deleted = int(cur.rowcount) if cur.rowcount and cur.rowcount > 0 else 0
    con.commit()
    return {"case_id": case_id, "session": sid, "turn": turn, "step": step,
            "uniq": uniq, "node": int(node), "body": body,
            "scratch_deleted": deleted, "already_closed": False}


# ── 还没 close 的 case ────────────────────────────────────────────────
def pending(con: sqlite3.Connection) -> list[dict]:
    """还没 close 的 case。scratch 里只放未关闭的，所以这里就是全部 case。"""
    ensure(con)
    rows = con.execute(
        "SELECT session, turn, step, kind, body, created FROM scratch ORDER BY id").fetchall()
    cases: dict[tuple[str, int, int], dict] = {}
    for r in rows:
        key = (str(r["session"]), int(r["turn"]), int(r["step"]))
        case = cases.get(key)
        if case is None:
            case = cases[key] = {"case_id": _case_id(*key), "session": key[0],
                                 "turn": key[1], "step": key[2], "notes": [],
                                 "created": str(r["created"])}
        case["notes"].append({"kind": str(r["kind"]), "body": str(r["body"])})
        case["created"] = str(r["created"])
    return list(cases.values())
