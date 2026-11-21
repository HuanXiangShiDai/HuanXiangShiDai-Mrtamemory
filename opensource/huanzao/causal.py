"""因果拼装：他说一句话，命中的记忆怎么拼成「因为…所以…」。

不是把记忆一条条并列摆出来——那是检索结果，不是上下文。
这里要的是**前后**：哪件事在前、哪张卡是它沉淀下来的道理、两段往事是不是同一回事的两个面。

拼法只有四种，都从图里算，不编：

1. **往事 → 卡**：往事是她经历的，卡是她从那件事里留下的道理。串成「因为经历过，所以如今这样」。
2. **往事 → 往事（同一链）**：两段挂在同一条 `next` 链上，就是一个动作的两步。
3. **往事 → 往事（共现）**：共享两个以上元素，是同一件事的两个面。
4. **只有卡**：她只有道理，没有具体那一回——这一句要老实说出来，不能装作想起来了。

时间顺序一律按 `order_key`：先天设定 → 生前 → 乐土各章 → 现在各天。
输出带层标（`头段·我知道｜人物关系` / `内容段·某处旧址-02`），和提醒块用的是同一套标签。
"""
from __future__ import annotations

import sqlite3

from . import retrieve

UP = "↑"          # 往事 → 道理
THEN = "→"        # 时间相续
SAME = "≈"        # 同一件事的两个面


def _layer(con: sqlite3.Connection, mem: int) -> str:
    row = con.execute("SELECT provenance FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    prov = str(row["provenance"] or "") if row else ""
    if prov.startswith("canon"):
        grp = prov.split(":", 1)[-1] or "设定"
        return f"头段·我知道｜{grp}"
    when = retrieve.describe_when(con, mem) or "往事"
    return f"内容段·{when}"


def _is_card(con: sqlite3.Connection, mem: int) -> bool:
    row = con.execute("SELECT provenance FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    return bool(row and str(row["provenance"] or "").startswith("canon"))


def _order(con: sqlite3.Connection, mem: int) -> str:
    row = con.execute("SELECT order_key FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    return str(row["order_key"] or "") if row else ""


def _gist(con: sqlite3.Connection, mem: int, limit: int = 60) -> str:
    m = retrieve.render_mem(con, mem)
    text = (m["text"] or m["title"]).strip()
    who = retrieve.describe_view(con, mem)
    if who:
        text = f"（{who}的视角）" + text
    return text[:limit] + ("…" if len(text) > limit else "")


def _shared(con: sqlite3.Connection, a: int, b: int) -> list[str]:
    rows = con.execute(
        "SELECT n.body FROM edges e1 JOIN edges e2 ON e1.dst = e2.dst "
        "JOIN nodes n ON n.id = e1.dst "
        "WHERE e1.src=? AND e2.src=? AND e1.rel IN ('has','has:lead') "
        "AND e2.rel IN ('has','has:lead') AND n.kind IN ('entity','affect','person') "
        "AND LENGTH(n.body) >= 2", (a, b)).fetchall()
    return [str(r["body"]) for r in rows]


def _in_chain(con: sqlite3.Connection, a: int, b: int) -> bool:
    row = con.execute(
        "SELECT 1 FROM edges WHERE rel='next' AND ((src=? AND dst=?) OR (src=? AND dst=?)) LIMIT 1",
        (a, b, b, a)).fetchone()
    return bool(row)


def _memories(con: sqlite3.Connection, text: str, k: int = 3) -> list[int]:
    """先按「这句话真提到的」找，再按结构找；往事与卡分开取。

    最后一道闸：**正文里必须真的出现那个元素**。结构上连得上不等于这段在说她问的事——
    「丙是个什么样的人」曾经把一段讲学历的往事带进来，只因为它挂着「丙」这个元素。
    """
    hits = retrieve.hit_elements(con, text)
    ids = [int(r["id"]) for r in hits]
    if not ids:
        return []
    surfaces = [str(r["body"]) for r in hits if len(str(r["body"])) >= 2]
    from . import diffuse as diffuse_mod
    cand = [m["mem"] for m in diffuse_mod.pick(con, ids, top=k + 4)]
    if len(cand) < k:
        for p in retrieve.memories_of(con, ids, limit=k + 4):
            if p["mem"] not in cand:
                cand.append(p["mem"])
    kept: list[tuple[int, int]] = []
    for m in cand:
        row = con.execute(
            "SELECT first_person, raw, body FROM mem_src s JOIN nodes n ON n.id = s.mem_node "
            "WHERE s.mem_node=?", (m,)).fetchone()
        blob = "".join(str(row[c] or "") for c in ("first_person", "raw", "body")) if row else ""
        lead = {str(r["body"]) for r in con.execute(
            "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
            "WHERE e.src=? AND e.rel='has:lead'", (m,))}
        # 「在说这件事」的判据不是「提到过」，是**密度**：命中几个词、是不是主元素、开没开篇。
        score = 0
        score += 2 * sum(1 for s in surfaces if s in blob)
        score += 2 * sum(1 for s in surfaces if s in lead)
        score += 1 if any(s in blob[:60] for s in surfaces) else 0
        if score >= 2:
            kept.append((score, m))
        if len(kept) >= k + 2:
            break
    kept.sort(key=lambda x: -x[0])
    return [m for _, m in kept[:k]]


def build(con: sqlite3.Connection, text: str, limit: int = 2) -> dict:
    """返回 {'lines': [...], 'causal': '...', 'chain': [...]}。"""
    mems = _memories(con, text, k=limit + 1)
    cards = [c["mem"] for c in retrieve.cards_for_text(con, text, limit=1)]
    if not mems and not cards:
        return {"lines": [], "causal": "", "why": "", "rel": "", "chain": []}

    # 往事按时间排；卡放在它沉淀出来的位置（最后）
    past = [m for m in mems if not _is_card(con, m)]
    past.sort(key=lambda m: _order(con, m))
    past = past[:limit]

    lines: list[str] = []
    chain: list[dict] = []
    for m in past:
        lines.append(f"· 【{_layer(con, m)}】{_gist(con, m)}")
        chain.append({"mem": m, "layer": _layer(con, m), "kind": "past"})
    for m in cards:
        lines.append(f"· 【{_layer(con, m)}】{retrieve.render_mem(con, m)['title']}")
        chain.append({"mem": m, "layer": _layer(con, m), "kind": "card"})

    # ── 关系：四种，谁先命中的规则谁生效 ──
    rel, why = "", ""
    if past and cards:
        rel, why = UP, "这段经历，就是那张卡现在还成立的原因"
    elif len(past) >= 2:
        a, b = past[0], past[1]
        if _in_chain(con, a, b):
            rel, why = THEN, "两段在同一条链上，前一件是后一件的来处"
        else:
            shared = _shared(con, a, b)
            if len(shared) >= 2:
                rel, why = SAME, "两段共享「" + "、".join(shared[:3]) + "」，是同一件事的两个面"
            else:
                rel, why = THEN, "按时间先后"
    elif cards and not past:
        rel, why = "…", "她只有这条道理，没有留下具体那一回"
    elif past:
        rel, why = "…", "她记得这一回，但这件事没有沉淀成话"

    causal = ""
    if rel and rel != "…":
        causal = f"【因果 {rel}】{why}。"
    elif why:
        causal = f"【因果】{why}。"
    return {"lines": lines, "causal": causal, "why": why, "rel": rel, "chain": chain}


def render(con: sqlite3.Connection, text: str, limit: int = 2) -> str:
    got = build(con, text, limit=limit)
    if not got["lines"]:
        return ""
    head = "【他这句话唤起的东西】"
    body = "\n".join(got["lines"])
    tail = got["causal"]
    return "\n".join(x for x in (head, body, tail) if x)


# ── 近期对话的隐性前提 + 缓存友好的拼装 ────────────────────────────
# 三件东西拼进同一个块：
#   ① 上一轮已经在这个块里的条目（**原样、原顺序**排最前）——前缀因此每轮都能长一截
#   ② 本轮新命中的（追加在后面）
#   ③ 此刻与上次说话的时刻（**放最后**，因为它每轮都变，放前面等于每轮废掉整个前缀）

def _now_line(con: sqlite3.Connection) -> str:
    from datetime import datetime
    now = datetime.now().astimezone()
    stamp = f"{now:%Y-%m-%d %H:%M}"
    last = None
    try:
        row = con.execute("SELECT MAX(created) c FROM current_turn").fetchone()
        if row and row["c"]:
            last = str(row["c"])
    except Exception:
        pass
    if not last:
        row = con.execute(
            "SELECT MAX(when_iso) w FROM mem_src WHERE order_key LIKE '3:%' "
            "AND when_iso LIKE '20%'").fetchone()
        last = str(row["w"]) if row and row["w"] else ""
    if not last:
        return f"【此刻】{stamp}"
    try:
        was = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if was.tzinfo is None:
            was = was.astimezone()
        days = (now.date() - was.date()).days
        gap = "今天" if days == 0 else ("昨天" if days == 1 else f"{days} 天前")
        return f"【此刻】{stamp}；上次说话 {was:%m-%d %H:%M}，{gap}"
    except Exception:
        return f"【此刻】{stamp}"


def _hot(con: sqlite3.Connection, k: int = 3) -> list[str]:
    """最近挂在心上的：先用脉冲（激活层），没有就用最近几条「现在」段的元素。"""
    try:
        from . import rules as rules_mod
        skip = rules_mod.op_words(con) | rules_mod.topic_words(con) | retrieve.NOISE | retrieve.META
    except Exception:
        skip = set()
    out: list[str] = []

    def take(body: str) -> None:
        b = str(body)
        if len(b) >= 2 and b not in out and b not in skip:
            out.append(b)

    try:
        from . import activation
        vals = activation.values(con)
        for nid, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:k]:
            row = con.execute("SELECT body FROM nodes WHERE id=?", (nid,)).fetchone()
            if row:
                take(str(row["body"]))
    except Exception:
        pass
    if not out:
        rows = con.execute(
            "SELECT src FROM edges WHERE rel IN ('has','has:lead') AND src IN ("
            "  SELECT mem_node FROM mem_src WHERE order_key LIKE '3:%' "
            "  ORDER BY order_key DESC LIMIT 3) "
            "AND dst IN (SELECT id FROM nodes WHERE kind IN ('entity','affect'))").fetchall()
        for r in rows:
            row = con.execute("SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
                              "WHERE e.src=? AND e.rel IN ('has','has:lead') "
                              "ORDER BY LENGTH(n.body) DESC LIMIT 1", (int(r["src"]),)).fetchone()
            if row:
                take(str(row["body"]))
            if len(out) >= k:
                break
    return out[:k]


def build_block(con: sqlite3.Connection, text: str, prev_ids: list[int] | None = None,
                limit: int = 1, full: bool = False, cap_lines: int = 4,
                max_chars: int = 900) -> dict:
    """拼一块能直接写进 messages 的上下文。

    顺序是**有意**的：上一轮沿用的 → 本轮新命中 → 因果 → 此刻。
    前三段尽量与上一轮重合，最后一行每轮都变——把变化压在尾巴上。

    两条约束：
    - `cap_lines`：最多几条记忆；超了先丢「沿用」段最旧的那条（它在稳定段尾部，破坏面最小）。
    - `max_chars`：块有软上限。超了先丢沿用条目，还超就把「原文全文」降级成截断版——
      否则连聊十轮，这个块会把上下文吃掉一大半。
    """
    got = build(con, text, limit=limit)
    fresh = [c["mem"] for c in got["chain"]]
    prev = [int(x) for x in (prev_ids or [])]

    order: list[int] = []
    for m in prev + fresh:                      # 上一轮在前，新的在后，去重保序
        if m not in order and con.execute("SELECT 1 FROM nodes WHERE id=?", (m,)).fetchone():
            order.append(m)

    def line_of(mem: int, use_full: bool) -> str:
        layer = _layer(con, mem)
        if _is_card(con, mem):
            return f"· 【{layer}】{retrieve.render_mem(con, mem)['title']}"
        if use_full:                             # 原文：能直接写进 messages 的那一份
            row = con.execute(
                "SELECT first_person, raw FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
            body = str((row["first_person"] or row["raw"] or "") if row else "").strip()
            who = retrieve.describe_view(con, mem)
            if who:
                body = f"（{who}的视角）" + body
            return f"· 【{layer}】{body}"
        return f"· 【{layer}】{_gist(con, mem, 120)}"

    def assemble(ids: list[int], use_full: bool) -> list[str]:
        # 不加「本轮／沿用」标记：那标记每轮都会翻面，翻一次就把那一行改掉、前缀白省。
        # 「哪条是新的」交给因果行去说（它本来就在最后，而且带「最后那条」）。
        return [line_of(m, use_full) for m in ids]

    stale_note = ""
    while True:
        ids = order[:cap_lines]
        lines = assemble(ids, full)
        head = "【他这句话唤起的东西】"
        hot = _hot(con, 3)
        if hot:
            head += " 近期挂在心上：" + "、".join(hot)
        parts = [head] + lines
        if got["causal"]:
            kept_old = sum(1 for m in ids if m not in fresh)
            parts.append(got["causal"] if not kept_old
                         else got["causal"].replace("【因果", "【因果·最后那条", 1))
        parts.append(_now_line(con))
        block = "\n".join(parts)
        if len(block) <= max_chars or (not full and len(ids) <= 1):
            break
        if full:
            full = False                        # 先降级成截断版
            continue
        stale = [m for m in ids if m not in fresh]
        if stale:
            order.remove(stale[-1])             # 再丢沿用段最旧的一条
            continue
        if len(ids) > 1:
            order.pop()                         # 最后才动本轮自己
            continue
        break
    return {"block": block, "chain": ids, "fresh": fresh,
            "hot": hot, "causal": got["causal"], "chars": len(block)}
