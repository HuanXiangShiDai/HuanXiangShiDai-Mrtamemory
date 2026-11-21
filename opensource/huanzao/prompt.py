"""装配：把图和记忆拼成一段字节。

三段布局：

    头段（会话头一次定死，之后一字不动 —— 缓存认的就是它）
        中心：她是谁、三条生长素、别名
        先天：设定类认知
        骨干：能带住最多记忆的那些元素
    尾段（每轮追加，不回头改）
        当下：这一轮被点亮的记忆
        台词：用户这句
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

from . import retrieve

HEAD_LIMIT_INNATE = 14   # 默认值；实际从 rule|常驻取舍 读
HEAD_LIMIT_BACKBONE = 16


def build_head(con: sqlite3.Connection) -> str:
    # 取几条、规矩写什么，都从图里读——改节点就改说法
    try:
        from . import rules as rules_mod
        _pick = rules_mod.head_pick(con)
        lim_in = int(_pick.get("innate", HEAD_LIMIT_INNATE))
        lim_back = int(_pick.get("backbone", HEAD_LIMIT_BACKBONE))
        manners = rules_mod.manners(con)
        _row = con.execute("SELECT body FROM nodes WHERE kind='mem' AND uniq='mem|manners'").fetchone()
        if _row:
            _leaves = [r["body"] for r in con.execute(
                "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
                "WHERE e.src=? AND e.rel='next' ORDER BY n.id",
                (con.execute("SELECT id FROM nodes WHERE uniq='mem|manners'").fetchone()["id"],))]
            manners = _leaves or [str(_row["body"])]
    except Exception:
        lim_in, lim_back, manners = HEAD_LIMIT_INNATE, HEAD_LIMIT_BACKBONE, []
    center = con.execute("SELECT id, body FROM nodes WHERE kind='center' LIMIT 1").fetchone()
    lines: list[str] = []
    aliases: list[str] = []
    if center:
        lines.append(f"【我是谁】{center['body']}。")
        for r in con.execute(
            "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
            "WHERE e.src=? AND e.rel='axis' ORDER BY n.id", (center["id"],)):
            lines.append(f"· {r['body']}")
        aliases = [r["body"] for r in con.execute(
            "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
            "WHERE e.src=? AND e.rel='alias' ORDER BY n.id", (center["id"],))]
        if aliases:
            lines.append("别人叫我：" + "、".join(aliases) + "。")

    try:
        from . import activation as act_mod
        _acts = act_mod.values(con)
    except Exception:
        _acts = {}
    # 头段必须**一次定死**：缓存认的是它，任何随运行状态漂移的排序都会让指纹天天变、
    # 前缀全废。所以这里只按 innate 边的建立顺序取（那顺序就是「我知道」的优先级），
    # 不掺激活值。
    #
    # 人格修订的位置继承：被 `supersedes` 的旧卡不出现，**由取代它的新卡原地顶替**。
    # 否则新卡排在 innate 边末尾，二十四个名额一满它就永远进不了头段——
    # 「人格修好了，但看不见」等于没修。
    _all = con.execute(
        "SELECT n.id, n.body, e.id AS eid FROM edges e JOIN nodes n ON n.id=e.dst "
        "WHERE e.rel='innate' ORDER BY e.id").fetchall()
    _sup = {int(r["dst"]): int(r["src"]) for r in con.execute(
        "SELECT src, dst FROM edges WHERE rel='supersedes'")}      # 旧 -> 新
    _by_id = {int(r["id"]): r for r in _all}
    _order: list[int] = []
    for r in _all:
        nid = int(r["id"])
        if nid in _sup:                      # 旧卡：让位给新卡
            new = _sup[nid]
            if new in _by_id and new not in _order:
                _order.append(new)
            continue
        if nid not in _order:
            _order.append(nid)
    for r in _all:                           # 新卡若还没排进去，补在末尾
        nid = int(r["id"])
        if nid not in _order and nid not in _sup:
            _order.append(nid)
    _rows = [_by_id[i] for i in _order if i in _by_id]
    innate = _rows[:lim_in]
    if innate:
        lines.append("\n【我知道】")
        for r in innate:
            lines.append("· " + str(r["body"])[:40])

    back = con.execute(
        "SELECT n.body, d.df FROM derived_df d JOIN nodes n ON n.id=d.node_id "
        "WHERE n.kind IN ('entity','affect') AND d.df >= 2 ORDER BY d.df DESC, n.id LIMIT ?",
        (lim_back * 3,)).fetchall()
    if back:
        names = [center["body"] if center else ""] + aliases
        # 「记忆」「规则」这种是谈系统的词，不是挂在心上的东西
        try:
            from . import retrieve as retrieve_mod
            from . import rules as rules_mod
            skip = (rules_mod.op_words(con) | rules_mod.topic_words(con)
                    | retrieve_mod.NOISE)
        except Exception:
            skip = set()
        picked: list[str] = []
        for r in back:
            b = str(r["body"])
            if b in names or b in picked or len(b) < 2 or b in skip:
                continue
            if re.search(r"(年|月|日|号|点|分)$", b) or b.startswith("心里想"):
                continue
            picked.append(b)
            if len(picked) >= lim_back:
                break
        if picked:
            lines.append("\n【常在我心上的】")
            lines.append("、".join(picked) + "。")

    lines.append("\n【规矩】")
    for one in (manners or [
        "只说我真记得的事。没经历过的，就直接说不记得、不知道，不许编。",
        "记不清的细节，说不记得细节，不要拿想象补上。",
        "宁可少说一句，也不要编一句。",
    ]):
        lines.append("· " + one)
    return "\n".join(lines).strip()


def render_tail(con: sqlite3.Connection, say: str, picked: list[dict],
                per_mem_chars: int = 45) -> str:
    """每条记忆留多少字，也从图里读（rule|提醒措辞 的 per_mem）。"""
    parts: list[str] = []
    try:
        from . import rules as rules_mod
        _style = rules_mod.remind_style(con)
    except Exception:
        _style = {}
    try:
        _pm = int(_style.get("per_mem") or 0)
        # per_mem=0 表示不截断：命中的记忆整段写进去
        per_mem_chars = 0 if _pm <= 0 else max(12, _pm // 3)
    except Exception:
        pass
    title = str(_style.get("title") or "【此刻浮上来的】")
    footer = str(_style.get("footer") or "（这些是你心里浮上来的，说不说由你；不合适就放过。）")
    if picked:
        parts.append(title)
        for i, p in enumerate(picked, 1):
            if p.get("kind") == "card":
                # 头段的东西被这句话牵下来了：标清它是「我知道」，不是往事。
                # 旁边还有往事时只报名字——卡是补充，别把往事挤掉。
                grp = str(p.get("group") or "设定")
                gist = str(p.get("gist") or "").strip()
                if per_mem_chars:
                    gist = gist[:per_mem_chars * 3]
                alone = len(picked) == 1
                parts.append(f"{i}. 【头段·我知道｜{grp}】{p.get('title', '')}"
                             f"{'：' + gist if (gist and alone) else ''}")
                continue
            focus = retrieve.focus_text(con, p.get("focus_id", 0)) if p.get("focus_id") else ""
            m = retrieve.render_mem(con, p["mem"], focus=focus)
            full = m["text"] or m["title"]
            text = full if not per_mem_chars else full[:per_mem_chars * 3]
            # 归属：不是她的视角，就别让她当成自己的记忆
            try:
                from . import view as view_mod
                who = view_mod.view_of(con, p["mem"])
                if who and who != "中心人物" and who != "未知":
                    text = f"（{who}的视角）" + text
            except Exception:
                pass
            # 层与位次：这条是内容段（往事），落在哪一章 / 哪一天
            try:
                when = retrieve.describe_when(con, p["mem"]) or "往事"
            except Exception:
                when = "往事"
            parts.append(f"{i}. 【内容段·{when}】{text}")
        parts.append(footer)
    parts.append("\n" + say)
    return "\n".join(parts)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """中文约 1.5 字一个 token。"""
    return int(len(text) / 1.5)
