"""把"思考的结论"变成真记忆。

之前的问题（实测发现的）：四段总结是**格式**，不是机制。
`digest` 把同一条思考按 condition/judge/conclusion 轮转填充，于是：

    一、在什么条件下：<整段思考>
    二、用了什么规则：（没记）
    三、对什么做了判别：<又是整段思考>
    四、得出什么结论：（没记）

第二、四段永远是空的。所以"思考得出过什么结论"这件事，在图上根本不存在。

这一层把它做实。四段各自有归宿：

    一、条件      → **丢掉**（它只对当时那一刻有意义，事后无用）
    二、规则      → 抽规则名 → 让元素指向规则（`refers`），结论因此可追溯到前提
    三、判别      → 与结论合成**一条常规记忆**（mem + mem_src + has 边）
    四、结论      → 同上

于是"结论"是可召回的，"前提规则"是可反查的。这正好回答一个问题：
**她凭什么这么想，以及这么想对不对。**

原文不动：`thought` 节点照旧保留。
"""
from __future__ import annotations

import re
import sqlite3

from . import db, rule_graph

# 结论句的标志词：从强到弱。只认这些，不猜。
CONCLUSION_MARKS = ["所以", "因此", "可见", "结论是", "我决定", "我认定", "说到底",
                    "归根到底", "这意味着", "那就是", "我想通了", "我明白"]
# 判别句的标志词
JUDGE_MARKS = ["其实", "看来", "说明", "并不是", "不是", "而是", "与其说", "更像",
               "判断", "算得上", "算不上", "应当", "应该"]
SPLIT = re.compile(r"[。！？\n]|(?<=[；])")
PROVENANCE = "thought-conclusion"

# 这套处理方式本身也进图：`rule|思考结论`。于是"思考怎么落成记忆"是图能管的事，
# 而不是代码里的常量——它同样可以被触发规则改写（和元素选取、触发条件一样）。
RULE_NAME = "思考结论"
RULE_BODY = ("思考结论（思考怎么落成记忆）：keep=judge,conclusion；drop=condition；"
             "rules=rules→refers；mem=常规记忆；min_len=8")


def ensure_rule(con: sqlite3.Connection) -> bool:
    """把处理规则建进图。幂等。"""
    if db.get_node(con, "rule|" + RULE_NAME):
        return False
    db.node(con, "rule", RULE_BODY, uniq="rule|" + RULE_NAME)
    con.commit()
    return True


def rule_of(con: sqlite3.Connection) -> dict:
    """读图里的处理规则；不在就用默认。"""
    try:
        from . import rule_graph
        body = rule_graph.current(con, RULE_NAME) or RULE_BODY
        kv = dict(rule_graph.parse_body(body).get("kvs", []))
    except Exception:
        kv = {}
    return {"keep": kv.get("keep", "judge,conclusion"),
            "drop": kv.get("drop", "condition"),
            "min_len": int(kv.get("min_len", 8) or 8)}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SPLIT.split(text) if s and len(s.strip()) >= 4]


def _rules_in(con: sqlite3.Connection, text: str) -> list[str]:
    """文本里点到了哪些规则（配置型 + 触发型，取短名）。"""
    out: list[str] = []
    for r in con.execute(
            "SELECT uniq FROM nodes WHERE kind='rule' AND uniq NOT LIKE '%|v%'").fetchall():
        uniq = str(r["uniq"])
        name = uniq.split("|", 1)[1]
        for cand in (name, name.replace("触发·", "")):
            if cand and cand in text and cand not in out:
                out.append(cand)
                break
    return out


def extract_parts(con: sqlite3.Connection, text: str) -> dict:
    """从自由文本里抽出规则 / 判别 / 结论。抽不到就返回空，不编。

    思考协议没改（模型仍自由输出），所以这里只能靠标志词 + 位置启发式：
    - 规则：直接匹配图里已有的规则名
    - 结论：含结论标志词的句子；没有就取最后一句
    - 判别：含判别标志词的句子；没有就用结论句之外最长的一句
    """
    sents = _sentences(text)
    if not sents:
        return {"rules": [], "judge": "", "conclusion": "", "ok": False}

    rules = _rules_in(con, text)

    conclusion = next((s for s in reversed(sents) if any(m in s for m in CONCLUSION_MARKS)), "")
    if not conclusion and sents:
        conclusion = sents[-1]

    judge = next((s for s in sents if s != conclusion and any(m in s for m in JUDGE_MARKS)), "")
    if not judge:
        rest = [s for s in sents if s != conclusion]
        judge = max(rest, key=len) if rest else ""

    return {"rules": rules, "judge": judge[:200], "conclusion": conclusion[:200],
            "ok": bool(conclusion)}


def worth_keeping(con: sqlite3.Connection, parts: dict) -> tuple[bool, str]:
    """这条结论值不值得变成记忆。

    三道门（宁可不写，也不要写一条空话进去）：
    1. 结论至少 8 字——「今天没什么特别的」这种不是结论，是流水账；
    2. 判别 + 结论里至少命中一个**图里有的元素**——没有元素它就召不回来，写进去是死数据；
    3. 结论与判别不能完全相同（否则等于没判别）。
    """
    c, j = parts.get("conclusion") or "", parts.get("judge") or ""
    rule = rule_of(con)
    if len(c) < rule["min_len"]:
        return False, "结论太短（< %d 字）" % rule["min_len"]
    if j and c and j == c:
        return False, "判别与结论同一句"
    try:
        from . import thought as thought_mod
        els = thought_mod._elements_in(con, "%s %s" % (j, c))
    except Exception:
        els = []
    if not els:
        return False, "没有任何图内元素可挂"
    return True, "可留（%d 个元素）" % len(els)


def _strip_label(s: str) -> str:
    """去掉总结里的段标签（「三、对什么做了判别：」），记忆正文里不该带它。"""
    return re.sub(r"^[一二三四]、[^：]{0,10}：", "", (s or "").strip())


def materialize(con: sqlite3.Connection, summary_uniq: str, text: str) -> dict:
    """把结论 + 判别写成一条**常规记忆**，并让它指向用到的规则。

    常规记忆的意思是：有 mem_src、有 has 边、进 derived_df、能被 flow/retrieve 召回。
    它和"往事"唯一的区别是 provenance 写着 `thought-conclusion`。
    """
    parts = extract_parts(con, text)
    if not parts["ok"]:
        return {"made": False, "why": "抽不到结论", "parts": parts}
    keep, why = worth_keeping(con, parts)
    if not keep:
        return {"made": False, "why": why, "parts": parts}

    body = "判别：%s。结论：%s。" % (_strip_label(parts["judge"]) or "（未区分）",
                                   _strip_label(parts["conclusion"]))
    uniq = "concl|" + summary_uniq.split("|", 1)[1]

    node = db.get_node(con, uniq)
    if node:
        mid = int(node["id"])
        made = False
    else:
        mid = db.node(con, "mem", body, uniq=uniq)
        con.execute(
            "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
            "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
            (mid, uniq, body, body, "conclusion", "live", "3:000:0000000000:00000000:0000000000:900000",
             PROVENANCE))
        made = True

    # 元素 → has：这样它才会被召回（和普通记忆走同一条路）。
    # 关键：结论里的实词**大多不在图里**（「被记住」「数清楚」这类组合词），
    # 只挂 `_elements_in` 能命中的那几个，结果就是挂 1 个元素、召不回来（实测）。
    # 所以这里用抽取器切一次，图里没有的**建点**——结论记忆得先有入口。
    linked_els: list[str] = []
    try:
        from . import extract as extract_mod
        _, items = extract_mod.run(body, "rules")
        for it in items[:10]:
            surface = str(it.get("surface") or "")
            if len(surface) < 2:
                continue
            row = con.execute("SELECT id FROM nodes WHERE body=? AND kind IN "
                              "('entity','affect','person') LIMIT 1", (surface,)).fetchone()
            if row:
                eid = int(row["id"])
            else:
                eid = db.node(con, "entity", surface, uniq="entity|" + surface)
            db.edge(con, mid, eid, "has")
            linked_els.append(surface)
    except Exception:
        try:
            from . import thought as thought_mod
            for el in thought_mod._elements_in(con, body)[:8]:
                db.edge(con, mid, el, "has")
        except Exception:
            pass

    # 规则 → refers：结论可反查到前提规则（元素是中介，这里直接指版本节点）
    linked = []
    for name in parts["rules"]:
        try:
            anchor = rule_graph._anchor_uniq(name)
            target = rule_graph._current_id(con, name) or \
                (rule_graph._node(con, anchor)["id"] if rule_graph._node(con, anchor) else None)
            if target:
                db.edge(con, mid, int(target), "refers")
                linked.append(name)
        except Exception:
            continue
    con.commit()
    # 新建的元素必须重算 df，否则它们在 `derived_df` 里没有行，
    # `hit_elements` 会把它们当孤儿跳过——结论记忆就永远召不回来（实测踩到）。
    try:
        from . import grow as grow_mod
        grow_mod.rebuild_df(con)
    except Exception:
        pass
    return {"made": made, "mem": mid, "uniq": uniq, "body": body,
            "rules": parts["rules"], "linked": linked, "elements": linked_els,
            "parts": parts}


def audit(con: sqlite3.Connection) -> dict:
    """有多少条结论记忆、多少条能反查到规则。"""
    total = con.execute(
        "SELECT COUNT(*) c FROM mem_src WHERE provenance=?", (PROVENANCE,)).fetchone()["c"]
    traced = con.execute(
        "SELECT COUNT(DISTINCT e.src) c FROM edges e JOIN mem_src s ON s.mem_node=e.src "
        "WHERE e.rel='refers' AND s.provenance=?", (PROVENANCE,)).fetchone()["c"]
    return {"conclusions": total, "traced_to_rule": traced,
            "untraced": total - traced}
