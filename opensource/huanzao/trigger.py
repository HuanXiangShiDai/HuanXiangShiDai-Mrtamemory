"""触发规则进图：让"什么情况触发什么"本身也是图里的节点。

之前 `autoself` 把触发条件写死在代码里：

    OP_WORDS = [("放宽", "thr↑"), ("收紧", "thr↓"), ...]      ← 图外常量

这有个致命后果：**它不在图里，所以它不能被查、不能被指、更不能被改。**
自指的环就断在这里——系统能改规则，但"什么时候改规则"由代码说了算。

现在把它搬进图：

    kind='rule'，uniq=`rule|触发·放宽`
    body：`触发·放宽：when=多记、调高；op=thr↑；key=auto`
              ↓ yields（触发词本身是元素）
          元素「多记」「调高」

于是：
- 触发规则能被 **元素** 命中（文本里出现「多记」→ 元素 → trigger）
- 它自己也是 `kind='rule'`，所以有版本链、极性账本、带符号环检测
- **它能被它自己触发的那套机制改写** —— 环在这里闭合

闭环长这样：

    思考 "我想多记几条" 
      → 元素「多记」
      → trigger|放宽（when 里就有「多记」）
      → thr↑ innate
      → 规则变
      → 下次思考"我想改触发条件" 
      → 元素「触发」→ trigger|改触发条件
      → 改的是 trigger 自己  ← δ* 再深一层
"""
from __future__ import annotations

import re
import sqlite3

from . import db, rule_c2, rule_graph

TRIGGER_PREFIX = "rule|触发·"

# 触发词 → 操作。搬进图之后，这张表只在 ensure() 里用一次。
DEFAULTS: list[tuple[str, str, str]] = [
    ("放宽", "多记、多留、放宽、调高、加大", "thr↑"),
    ("收紧", "少记、少留、收紧、调低、减小", "thr↓"),
    ("加停用", "不要再记、加进停用、别记住", "stop+"),
    ("移停用", "可以记、移出停用、允许记", "stop-"),
]

# 两条"特殊通道"：不是改某条规则，而是打开别的门。
# 它们也必须挂触发词元素，否则 `match`（走元素 + yields）看不见它们——
# 直接建点不挂边的话，规则在图里但永远命不中（实测踩到）。
SPECIALS: list[tuple[str, str, str]] = [
    ("主动自指", "我想改、让我调整、我需要修正、自指、做一次自指、自指一轮、再自指、自省", "manual"),
    ("夜间脉冲", "夜间、凌晨、三点了", "manual"),
]
# 它不是"又一个触发规则"，它让"触发条件"本身成为可被触发的对象。
META_NAME = "改触发"
META_WORDS = "改触发条件、去掉这个触发、改掉那条触发、这个触发条件、别再因为"
META_BODY = "触发·%s：when=%s；op=stop+；key=when" % (META_NAME, META_WORDS)


def _body(name: str, when: str, op: str) -> str:
    return "触发·%s：when=%s；op=%s；key=auto" % (name, when, op)


def ensure(con: sqlite3.Connection) -> dict:
    """把默认触发规则建进图，并把它们的触发词挂成元素。幂等。

    第 5 条（元触发）单独建：它 yield 的是**触发规则本身的名字**，
    所以它能被"改触发条件"这类话命中，进而去改前面那几条。
    """
    made, linked = [], 0
    for name, when, op in DEFAULTS:
        uniq = TRIGGER_PREFIX + name
        node = db.get_node(con, uniq)
        if not node:
            body = _body(name, when, op)
            # 直接建点，**不要**走 rule_graph.set_rule——
            # set_rule 会顺手建出 `rule|<name>|v1`，于是同一条触发规则出现两份
            # （`rule|放宽` 与 `trigger|放宽`），这是实测踩到的副作用。
            nid = db.node(con, "rule", body, uniq=uniq)
            node = db.get_node(con, uniq)
            made.append(name)
        # 触发词 → 元素 → yields
        for w in when.split("、"):
            w = w.strip()
            if len(w) < 2:
                continue
            el = con.execute("SELECT id FROM nodes WHERE body=? AND kind IN "
                             "('entity','affect') LIMIT 1", (w,)).fetchone()
            if not el:
                eid = db.node(con, "entity", w, uniq="entity|" + w)
            else:
                eid = int(el["id"])
            if node:
                db.edge(con, int(node["id"]), eid, "yields")
                linked += 1

    # —— 两条特殊通道：同样要挂触发词元素 ——
    for name, when, op in SPECIALS:
        uniq = TRIGGER_PREFIX + name
        node = db.get_node(con, uniq)
        if not node:
            db.node(con, "rule", "触发·%s：when=%s；op=%s；key=auto" % (name, when, op),
                    uniq=uniq)
            node = db.get_node(con, uniq)
            made.append(name)
        if node:
            for w in when.split("、"):
                w = w.strip()
                if len(w) < 2:
                    continue
                el = con.execute("SELECT id FROM nodes WHERE body=? AND kind IN "
                                 "('entity','affect') LIMIT 1", (w,)).fetchone()
                eid = int(el["id"]) if el else db.node(con, "entity", w, uniq="entity|" + w)
                db.edge(con, int(node["id"]), eid, "yields")
                linked += 1

    # —— 第 5 条：元触发 ——
    meta_uniq = TRIGGER_PREFIX + META_NAME
    meta = db.get_node(con, meta_uniq)
    if not meta:
        db.node(con, "rule", META_BODY, uniq=meta_uniq)
        meta = db.get_node(con, meta_uniq)
        made.append(META_NAME)
    if meta:
        for w in META_WORDS.split("、"):
            w = w.strip()
            if len(w) < 2:
                continue
            el = con.execute("SELECT id FROM nodes WHERE body=? AND kind IN "
                             "('entity','affect') LIMIT 1", (w,)).fetchone()
            eid = int(el["id"]) if el else db.node(con, "entity", w, uniq="entity|" + w)
            db.edge(con, int(meta["id"]), eid, "yields")
            linked += 1
        # 元触发指向那 4 条触发规则：它要改的就是它们
        for name, _, _ in DEFAULTS:
            t = db.get_node(con, TRIGGER_PREFIX + name)
            if t:
                db.edge(con, int(meta["id"]), int(t["id"]), "targets")
    # 补挂：规则被改过（when 里加了新词）之后，那些新词还没有元素和 yields 边。
    # 不补的话「规则改了但命不中」——看着对，实际死（实测踩到）。
    for t in all_triggers(con):
        for w in str(t["when"]).split("、"):
            w = w.strip()
            if len(w) < 2:
                continue
            el = con.execute("SELECT id FROM nodes WHERE body=? AND kind IN "
                             "('entity','affect') LIMIT 1", (w,)).fetchone()
            eid = int(el["id"]) if el else db.node(con, "entity", w, uniq="entity|" + w)
            have = con.execute("SELECT 1 FROM edges WHERE src=? AND dst=? AND rel='yields'",
                               (t["id"], eid)).fetchone()
            if not have:
                db.edge(con, int(t["id"]), eid, "yields")
                linked += 1
    con.commit()
    return {"triggers": made, "yields": linked,
            "total": con.execute("SELECT COUNT(*) c FROM nodes WHERE uniq LIKE ?",
                                 (TRIGGER_PREFIX + "%",)).fetchone()["c"]}


def all_triggers(con: sqlite3.Connection) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT id, uniq, body FROM nodes WHERE uniq LIKE ? AND uniq NOT LIKE '%|v%' "
            "ORDER BY id", (TRIGGER_PREFIX + "%",)).fetchall():
        uniq = str(r["uniq"])
        # 注意：`name` 是规则层的名字（`触发·放宽`），不是触发规则的短名（`放宽`）。
        # 剥错前缀会去查 `rule|放宽` 的 current——那是历史遗留的另一个锚点（实测踩到）。
        name = uniq[len("rule|"):] if uniq.startswith("rule|") else uniq
        # 读**生效版本**，不是锚点：触发规则被改过之后，锚点还留着旧正文，
        # 照锚点读就会出现「body 里已经没有『多记』了，match 却还认它」（实测踩到）。
        try:
            live = rule_graph.current(con, name)
        except Exception:
            live = None
        body = live or str(r["body"])
        parsed = rule_graph.parse_body(str(body))
        kv = dict(parsed.get("kvs", []))
        out.append({"id": int(r["id"]), "uniq": str(r["uniq"]),
                    "name": str(parsed.get("title", "")).replace("触发·", ""),
                    "when": kv.get("when", ""), "op": kv.get("op", ""),
                    "key": kv.get("key", "auto"), "body": str(r["body"])})
    return out


def match(con: sqlite3.Connection, text: str) -> list[dict]:
    """文本命中了哪些触发规则——**走元素**，不查代码常量。

    按命中词的**长度降序**返回：词越长越锐，越该优先。
    实测踩到：文本「去掉「放宽」这个触发条件」同时含「多记」和「这个触发条件」，
    按 id 顺序会先命中「放宽」，元触发永远轮不到——环就合不上。
    """
    hits = []
    for t in all_triggers(con):
        words = [w.strip() for w in t["when"].split("、") if len(w.strip()) >= 2]
        best = None
        for w in words:
            if w in text and (best is None or len(w) > len(best)):
                el = con.execute("SELECT id FROM nodes WHERE body=? LIMIT 1", (w,)).fetchone()
                if not el:
                    continue
                yes = con.execute(
                    "SELECT 1 FROM edges WHERE src=? AND dst=? AND rel='yields'",
                    (t["id"], int(el["id"]))).fetchone()
                if yes:
                    best = w
        if best:
            hits.append(dict(t, matched=best))
    hits.sort(key=lambda h: -len(h["matched"]))
    return hits


def fire(con: sqlite3.Connection, trigger: dict, summary: str, text: str,
         target_rule: str | None = None) -> dict:
    """按一条触发规则执行一次改写。参数尽量从文本里取，取不到就放弃。"""
    op = trigger["op"]
    key = trigger["key"]

    # —— 元触发：改的是另一条触发规则自己（环在这里合上）——
    if trigger["name"] == META_NAME:
        victim = None
        for t in all_triggers(con):
            if t["uniq"] == trigger["uniq"]:
                continue
            if t["name"] in text:                      # 直接点名优先
                victim = t
                break
        if victim is None:
            for t in all_triggers(con):
                if t["uniq"] == trigger["uniq"]:
                    continue
                if any(w in text for w in t["when"].split("、")):
                    victim = t
                    break
        if victim is None:
            return {"ok": False, "why": "没点名要改哪条触发规则"}
        word = next((w for w in victim["when"].split("、") if w in text), None)
        if not word:
            return {"ok": False, "why": "没说要删哪个触发词"}
        try:
            # 目标名要带「触发·」前缀：trigger 的 uniq 是 `rule|触发·放宽`，
            # 而 set_rule 按 `rule|<name>` 拼锚点。传 "放宽" 会去找不存在的 `rule|放宽`。
            anchor_name = trigger["name"] + "·" + victim["name"] if False else "触发·" + victim["name"]
            res = rule_c2.typed_write(con, summary, anchor_name, "stop+",
                                      {"key": "when", "w": word}, by="trigger",
                                      why="元触发 %s 去掉「%s」的触发词「%s」"
                                          % (trigger["uniq"], victim["name"], word))
            return {"ok": True, "meta": True, "trigger": trigger["uniq"],
                    "victim": victim["uniq"], "removed": word,
                    "version": res.get("version"), "enacts": res.get("enacts")}
        except Exception as e:
            return {"ok": False, "trigger": trigger["uniq"], "why": str(e)[:90]}

    # —— 两条特殊通道：它们不是"改某条规则"，而是打开别的门 ——
    if trigger["name"] == "夜间脉冲":
        from . import night
        res = night.run_window(con)
        return {"ok": bool(res.get("ran")), "channel": "night",
                "why": res.get("why") or "窗口内已跑", "report": res.get("did")}
    if trigger["name"] == "主动自指":
        # 这道门是"我自己从无到有地自指一轮"：读图里的判据，报出该往哪走。
        # 它**不改任何东西**——真改动走两条正门：
        #   门 1〔自指〕段（模型亲手指，最准）；
        #   门 2 收口时 `stream.close` → `autoself.run`（没写门 1 也兜得住）。
        # 以前这里只回一句"请去写门 1"，于是被要求自指 = 什么都不发生。
        from . import selfprobe
        return selfprobe.probe(con)

    rule_name = target_rule
    if not rule_name:
        # 目标规则：文本里点到了哪条既有规则
        names = [str(r["uniq"]).split("|")[1] for r in con.execute(
            "SELECT uniq FROM nodes WHERE kind='rule' AND uniq NOT LIKE '%|v%' "
            "AND uniq NOT LIKE ?", (TRIGGER_PREFIX + "%",)).fetchall()]
        rule_name = next((n for n in names if n in text), None)
    if not rule_name:
        return {"ok": False, "why": "文本里没点到任何规则，不猜"}

    payload: dict = {}
    if op in ("thr↑", "thr↓"):
        body = rule_graph.current(con, rule_name)
        keys = [k for k, _ in rule_graph.parse_body(body).get("kvs", [])]
        k = key if key != "auto" else next((x for x in keys if x in text), keys[0] if keys else "")
        if not k:
            return {"ok": False, "why": "没有可调的键"}
        m = re.search(re.escape(trigger["matched"]) + r"[^0-9]{0,14}(\d+(?:\.\d+)?)", text)
        if not m:
            m = re.search(re.escape(k) + r"[^0-9]{0,14}(\d+(?:\.\d+)?)", text)
        if not m:
            return {"ok": False, "why": "没抽到数值，不猜"}
        payload = {"key": k, "to": m.group(1)}
    elif op in ("stop+", "stop-"):
        # 要停用的词：优先取引号里那个——「把「不要」加进停用」里的「不要」。
        # 退而求其次取触发词**前面**的短片段；`split(...)[-1]` 取到的是后半句，
        # 会把"从操作词里去掉"当成要停用的词（实测踩到）。
        m = re.search(r"[「『\"“]([^」』\"”]{1,8})[」』\"”]", text)
        if m:
            w = m.group(1)
        else:
            head = text.split(trigger["matched"])[0].strip(" ，。：、")
            w = head[-6:] if head else ""
        if not w:
            return {"ok": False, "why": "没抽到停用词"}
        payload = {"w": w}
    else:
        return {"ok": False, "why": "该操作暂不自动（需要两头词）"}

    try:
        res = rule_c2.typed_write(con, summary, rule_name, op, payload, by="trigger",
                                  why="触发规则 %s（命中「%s」）"
                                      % (trigger["uniq"], trigger["matched"]))
        return {"ok": True, "trigger": trigger["uniq"], "rule": rule_name,
                "op": op, "payload": payload, "version": res.get("version"),
                "enacts": res.get("enacts")}
    except Exception as e:
        return {"ok": False, "trigger": trigger["uniq"], "why": str(e)[:90]}
