"""自指的触发链路：把"想过什么"自动接到"改了什么"。

之前的状态：`scratch`（临时态）、`thought`（思考落图）、`rule_c2`（类型化改写）、
`persona`（人格提案）**四个零件各自完好，但没有任何东西把它们串起来**——
要用的时候得人工敲四条命令。那不是链路，是装配线。

这一层负责串：

    thought 落图
      ↓ 一步结束（或攒够 N 条）
    digest()      把这一步的思考聚成一个 case：open → note → close → 一条总结
      ↓
    parse_intents()  从总结里"指"出意图（不是生成正文）
      ├─ 规则意图 → rule_c2.typed_write   ← 只能指 (规则, 操作, 参数)
      └─ 人格意图 → persona.propose        ← 只能给方向；到阈值才 apply
      ↓
    返回一份"这次做了什么"的账

**为什么解析必须保守**：C2 的立意是"思考只指不写"。所以这里只认三类东西——
既有的规则名、六种原子操作的自然语言别名、一个数字。
抽不出规则名 或 抽不出参数，就**放弃**，让它留在总结里当记录，不碰规则。
宁可不动，也不要猜。
"""
from __future__ import annotations

import re
import sqlite3

from . import db, persona, rule_c2, rule_graph, scratch

# 操作关键词 → 六种原子操作。注意：这里不做同义扩展，只认明确的说法。
OP_WORDS: list[tuple[str, str]] = [
    ("放宽", "thr↑"), ("调高", "thr↑"), ("加大", "thr↑"), ("多留", "thr↑"), ("多记", "thr↑"),
    ("收紧", "thr↓"), ("调低", "thr↓"), ("减小", "thr↓"), ("少留", "thr↓"), ("少记", "thr↓"),
    ("加进停用", "stop+"), ("加进屏蔽", "stop+"), ("不要再记", "stop+"),
    ("移出停用", "stop-"), ("可以记", "stop-"),
    ("合并", "merge"), ("拆成", "split"), ("拆开", "split"),
]

# 人格意图：只认"我变了"的明确说法
PERSONA_UP = ("我更", "我更要", "我想更", "我想把", "我决定更", "我要把它", "我想更看重")
PERSONA_DOWN = ("我不再", "我不想再", "我放下", "我不再那么")

NUM = re.compile(r"(\d+(?:\.\d+)?)")


def digest(con: sqlite3.Connection, session: str, turn: int, step: int,
           apply_persona: bool = True) -> dict:
    """把某一步的思考压成一条总结（走 scratch 正常流程，临时态随之清空）。"""
    rows = con.execute(
        "SELECT body FROM nodes WHERE kind='thought' AND uniq LIKE ? AND uniq NOT LIKE 'sum|%' "
        "ORDER BY id", ("thought|%s|%d|%d|%%" % (session, turn, step),)).fetchall()
    if not rows:
        return {"digest": None, "why": "这一步没有思考可收"}

    case = scratch.open_case(con, session, turn, step)
    kinds = ("condition", "judge", "conclusion")
    for i, r in enumerate(rows):
        scratch.note(con, case, kinds[i % len(kinds)], str(r["body"])[:200])
    closed = scratch.close_case(con, case)
    # 两个句柄别混：`digest` 是总结节点（sum|…），`case` 是 case id（session|turn|step）。
    # persona.propose 要的是 case id，typed_write 要的是总结 uniq（实测拼成 sum|sum|… 踩过）。
    return {"digest": closed["uniq"], "case": case, "node": closed["node"],
            "thoughts": len(rows), "summary": closed["body"]}


def _number_after(text: str, word: str) -> str | None:
    """取「关键词之后最近的一个数字」。

    不能直接 `findall` 取第一个：总结的标题里带「第 1 轮第 1 步」，
    第一个数字永远是那个序号，不是参数（实测踩到：把 innate 放宽到 26 抽成了 1）。
    """
    m = re.search(re.escape(word) + r"[^0-9]{0,14}(\d+(?:\.\d+)?)", text)
    return m.group(1) if m else None


def _rules_named_in(con: sqlite3.Connection, text: str) -> list[str]:
    """总结里点到了哪几条规则（只认既有的 10 条，不认新名字）。"""
    out = []
    for name in rule_yields_names(con):
        if name and name in text:
            out.append(name)
    return out


def rule_yields_names(con: sqlite3.Connection) -> list[str]:
    return [str(r["uniq"]).split("|")[1]
            for r in con.execute(
                "SELECT uniq FROM nodes WHERE kind='rule' AND uniq NOT LIKE '%|v%'").fetchall()]


def parse_intents(con: sqlite3.Connection, text: str) -> list[dict]:
    """从总结里指出规则意图：返回 [{rule, op, payload}]。

    **现在走图**：先查 `trigger|*` 里的触发规则（它们的 when 词挂成了元素），
    命中哪条就按它的 op 执行。抽不到触发规则、或抽不到参数，就返回空——
    宁可不动，也不要猜。
    """
    from . import trigger as trigger_mod
    intents: list[dict] = []
    hits = trigger_mod.match(con, text)
    for t in hits:
        intents.append({"rule": None, "op": t["op"], "payload": {},
                        "trigger": t["uniq"], "matched": t["matched"],
                        "why": "图内触发规则 %s（命中元素「%s」）" % (t["uniq"], t["matched"])})
    if intents:
        return intents                       # 图内为空时才退回旧的关键词表
    names = _rules_named_in(con, text)
    if not names:
        return intents
    words = [(w, op) for w, op in OP_WORDS if w in text]
    if not words:
        return intents
    for name in names:
        body = rule_graph.current(con, name)
        # 参数键：优先从当前正文里已有的键里找（不去猜新键）
        keys = [k for k, _ in rule_graph.parse_body(body).get("kvs", [])]
        key = next((k for k in keys if k in text), keys[0] if keys else "")
        for w, op in words:
            payload: dict = {}
            if op in ("thr↑", "thr↓"):
                num = _number_after(text, w) or _number_after(text, key or "到")
                if not key or not num:
                    continue                     # 抽不出参数就放弃
                payload = {"key": key, "to": num}
            elif op in ("stop+", "stop-"):
                w2 = text.split(w)[-1].strip(" ，。：")[:8]
                if not w2:
                    continue
                payload = {"w": w2}
            else:
                continue                          # merge/split 需要两头词，暂不自动
            intents.append({"rule": name, "op": op, "payload": payload, "why": "总结里点到了"}
                           )
            break
    return intents


def parse_persona(con: sqlite3.Connection, text: str) -> list[dict]:
    """从总结里指出人格意图：返回 [{canon, dir}]。

    匹配顺序（从准到宽）：
    1. 总结里直接写了 canon 卡的 uniq 名（如 `tab_canon_core_persona`）；
    2. 总结里出现了卡片正文的前 6 个字；
    3. 兜底：若只有一张"核心"卡，就用它——否则人格意图永远落不了地
       （实测：正文前 6 字是「核心：爱具体」，总结里根本不会这么写）。
    """
    out = []
    d = 0
    if any(w in text for w in PERSONA_UP):
        d = 1
    elif any(w in text for w in PERSONA_DOWN):
        d = -1
    if d == 0:
        return out

    cards = con.execute(
        "SELECT n.uniq, n.body FROM nodes n JOIN mem_src s ON s.mem_node=n.id "
        "WHERE s.provenance LIKE 'canon%' AND n.uniq NOT LIKE '%|r%'").fetchall()

    hit = None
    for r in cards:                                    # 1) uniq 名
        short = str(r["uniq"]).split("|")[-1]
        if short and short in text:
            hit = str(r["uniq"])
            break
    if not hit:
        for r in cards:                                # 2) 正文前 6 字
            head = str(r["body"])[:6]
            if head and head in text:
                hit = str(r["uniq"])
                break
    if not hit:                                        # 3) 唯一的"核心"卡兜底
        core = [str(r["uniq"]) for r in cards if "core" in str(r["uniq"])]
        if len(core) == 1:
            hit = core[0]

    if hit:
        out.append({"canon": hit, "dir": d, "how": "uniq" if hit in text else "兜底"})
    return out


def run(con: sqlite3.Connection, session: str, turn: int, step: int,
        apply: bool = True) -> dict:
    """一步的完整触发链：收总结 → 指意图 → 改规则 / 提人格案。

    apply=False 时只报告"会做什么"，一个字都不写（除了总结本身）。
    """
    got = digest(con, session, turn, step)
    if not got["digest"]:
        return {"step": (session, turn, step), "digest": None, "actions": []}
    text = got["summary"]

    actions = []
    for it in parse_intents(con, text):
        item = dict(it)
        if apply:
            try:
                if it.get("trigger"):        # 图内触发规则：交给 trigger.fire
                    from . import trigger as trigger_mod
                    t = next((x for x in trigger_mod.all_triggers(con)
                              if x["uniq"] == it["trigger"]), None)
                    if t is None:
                        item.update({"ok": False, "why": "触发规则不见了"})
                    else:
                        # matched 是 match() 时加上的，all_triggers 不带——补回去
                        res = trigger_mod.fire(con, dict(t, matched=it.get("matched", "")),
                                               got["digest"], text)
                        item.update(res)
                else:                        # 旧路径：直接 typed_write
                    res = rule_c2.typed_write(con, got["digest"], it["rule"], it["op"],
                                              it["payload"], by="autoself",
                                              why="自指链路：%s" % got["digest"])
                    item.update({"ok": True, "version": res.get("version"),
                                 "enacts": res.get("enacts")})
            except Exception as e:
                item.update({"ok": False, "error": str(e)[:80]})
        actions.append(item)

    for p in parse_persona(con, text):
        item = dict(p)
        if apply:
            try:
                persona.propose(con, got["case"], p["canon"], p["dir"],
                                why="自指链路：%s" % got["digest"])
                item.update({"ok": True, "tally": persona.tally(con, p["canon"])})
            except Exception as e:
                item.update({"ok": False, "error": str(e)[:80]})
        actions.append(item)

    return {"step": (session, turn, step), "digest": got["digest"],
            "thoughts": got["thoughts"], "actions": actions,
            "depth": rule_c2.depth(con)}
