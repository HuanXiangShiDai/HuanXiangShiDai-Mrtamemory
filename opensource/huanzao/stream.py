"""分段思考：把"边想边写"从整段落库升级成按段落地。

之前（v0.1）：`llm/stream` 每攒 600 字把整段思考写成一个 thought 节点。
问题：**结论要等整段想完才存在**，所以同一轮思考调用不到自己的结论。

现在：思考可以带分段标记，标记一到就落库——

    〔条件〕他在问那天晚上的事
    〔规则〕常驻取舍
    〔判别〕这件事该留下来，不是可选项
    〔结论〕那就留着，不改
    〔自指〕规则=常驻取舍 操作=thr↑ 参数=innate:28

四类段各去各的地方（和实证 003 一致），第五类 `自指` 是**模型主动发起改写**：
它只能写"哪条规则、哪个操作、什么参数"，正文由 C2 的闸门推导——碰不到正文。

标记缺席时完全退回旧行为（整段落库），所以老的思考流不受影响。
"""
from __future__ import annotations

import re
import sqlite3

from . import db, scratch, thought

# 段名 → scratch 的 note kind
MARKS: dict[str, str] = {
    "条件": "condition",
    "规则": "rule",
    "判别": "judge",
    "结论": "conclusion",
}
SELFREF = "自指"
MARK_RE = re.compile("〔(%s|%s)〕" % ("|".join(MARKS), SELFREF))

# 每个 (session,turn,step) 的解析缓冲。
# **必须持久化**：插件每 flush 一次就 spawn 一个 python 进程，进程内字典活不过一次调用。
# 早先用内存字典，结果跨进程时只有最后一段能配对，前几段全丢（实测踩到）。
def ensure(con: sqlite3.Connection) -> None:
    con.executescript(
        "CREATE TABLE IF NOT EXISTS stream_state ("
        "  session TEXT NOT NULL, turn INTEGER NOT NULL, step INTEGER NOT NULL,"
        "  buf TEXT NOT NULL DEFAULT '', pending TEXT NOT NULL DEFAULT '',"
        "  text TEXT NOT NULL DEFAULT '', marked INTEGER NOT NULL DEFAULT 0,"
        "  case_id TEXT NOT NULL DEFAULT '',"
        "  PRIMARY KEY(session, turn, step));")
    con.commit()


def _load(con, session, turn, step) -> dict:
    ensure(con)
    r = con.execute("SELECT * FROM stream_state WHERE session=? AND turn=? AND step=?",
                    (session, turn, step)).fetchone()
    if not r:
        return {"buf": "", "pending": "", "text": "", "marked": 0, "case": None,
                "landed": [], "selfref": []}
    return {"buf": str(r["buf"]), "pending": str(r["pending"]), "text": str(r["text"]),
            "marked": int(r["marked"]), "case": (str(r["case_id"]) or None),
            "landed": [], "selfref": []}


def _save(con, session, turn, step, st) -> None:
    con.execute(
        "INSERT INTO stream_state(session, turn, step, buf, pending, text, marked, case_id) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session,turn,step) DO UPDATE SET "
        "buf=excluded.buf, pending=excluded.pending, text=excluded.text, "
        "marked=excluded.marked, case_id=excluded.case_id",
        (session, turn, step, st["buf"], st.get("pending") or "", st["text"],
         int(bool(st["marked"])), st.get("case") or ""))
    con.commit()


def reset(con: sqlite3.Connection, session: str, turn: int, step: int) -> None:
    ensure(con)
    con.execute("DELETE FROM stream_state WHERE session=? AND turn=? AND step=?",
                (session, turn, step))
    con.commit()


def feed(con: sqlite3.Connection, session: str, turn: int, step: int,
         chunk: str, final: bool = False) -> dict:
    """喂一段流式文本。返回这一轮已落库的东西（给"同轮可见"用）。"""
    session = session or "live"
    st = _load(con, session, turn, step)
    st["buf"] += chunk or ""
    st["text"] += chunk or ""

    landed: list[dict] = []
    while True:
        m = MARK_RE.search(st["buf"])
        if not m:
            break
        head = st["buf"][:m.start()].strip()
        st["buf"] = st["buf"][m.end():]
        st["marked"] = 1
        if head and st.get("pending"):
            landed.append(_land(con, st, session, turn, step, st["pending"], head))
        st["pending"] = m.group(1)

    if final:
        tail = st["buf"].strip()
        if st.get("pending") and tail:
            landed.append(_land(con, st, session, turn, step, st["pending"], tail))
        elif tail and not st["marked"]:
            nid = thought.record(con, {"session": session, "turn": turn, "step": step,
                                       "seq": 0, "text": st["text"]}, bump=True)
            landed.append({"kind": "thought", "node": nid, "chars": len(st["text"])})
        st["buf"] = ""
        st["pending"] = ""

    _save(con, session, turn, step, st)
    return {"marked": bool(st["marked"]), "landed": landed}


def _land(con, st, session, turn, step, name, body) -> dict:
    body = body.strip()
    if not body:
        return {"kind": name, "chars": 0}
    if name == SELFREF:
        st.setdefault("selfref", []).append(body[:200])
        return {"kind": "selfref", "body": body[:120]}
    if not st.get("case"):
        st["case"] = scratch.open_case(con, session, turn, step)
    scratch.note(con, st["case"], MARKS[name], body[:400])
    return {"kind": name, "chars": len(body), "case": st["case"]}


def close(con: sqlite3.Connection, session: str, turn: int, step: int) -> dict:
    """收口：把 case 压成总结（结论同时落成记忆），并触发模型主动发起的自指。"""
    session = session or "live"
    st = _load(con, session, turn, step)
    case = st.get("case")
    out: dict = {"closed": False}
    if case:
        out.update({"closed": True, "summary": scratch.close_case(con, case)})

    # 模型在思考里写的〔自指〕段：解析成 (规则, 操作, 参数) 后交给 C2 闸门
    results = []
    for line in (st.get("selfref") or [])[-2:]:
        results.append(_fire_selfref(con, session, turn, step, line))
    if results:
        out["selfref"] = results
    reset(con, session, turn, step)
    return out


def _fire_selfref(con, session, turn, step, line: str) -> dict:
    """`规则=常驻取舍 操作=thr↑ 参数=innate:28` → typed_write。"""
    import re as _re
    from . import rule_c2
    kv = dict(_re.findall(r"([^\s=；]+)=([^\s；]+)", line))
    rule = kv.get("规则") or kv.get("rule")
    op = kv.get("操作") or kv.get("op")
    arg = kv.get("参数") or kv.get("arg") or ""
    if not rule or not op:
        return {"ok": False, "why": "自指段缺少规则或操作", "line": line[:60]}
    payload: dict = {}
    hit = _re.search(r"([A-Za-z_\u4e00-\u9fff]+)[:：](\d+(?:\.\d+)?)", arg)
    if hit:
        payload = {"key": hit.group(1), "to": hit.group(2)}
    elif arg and arg.isdigit():
        payload = {"w": arg}
    try:
        res = rule_c2.typed_write(con, "sum|%s|%d|%d" % (session, turn, step),
                                  rule, op, payload, by="model",
                                  why="模型在思考中主动发起")
        return {"ok": True, "rule": rule, "op": op, "version": res.get("version")}
    except Exception as e:
        return {"ok": False, "why": str(e)[:80]}


def pending_conclusions(con: sqlite3.Connection, session: str, turn: int, step: int) -> list[dict]:
    """本轮已经落库的结论——给"同轮可见"用。

    思考中途调这个，就能看见自己刚得出的结论（这正是 v0.1 做不到的事）。
    """
    case = "sum|%s|%d|%d" % (session, turn, step)
    rows = con.execute(
        "SELECT kind, body FROM scratch WHERE session=? AND turn=? AND step=? "
        "AND kind IN ('judge','conclusion') ORDER BY id",
        (session, turn, step)).fetchall()
    return [{"kind": str(r["kind"]), "body": str(r["body"])[:120]} for r in rows]
