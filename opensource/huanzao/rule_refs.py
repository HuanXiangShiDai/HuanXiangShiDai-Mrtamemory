"""元素指向规则：把 `kind='rule'` 从孤立点接进元素链。

为什么要有这个文件
------------------
规则是图里的点（`rule|常驻取舍`、`rule|不入记忆` …），可它们**一条边都没有**：
元素链（用户 → 喜欢 → 咖啡）永远走不到规则，于是「这条记忆碰到了哪条规则」
在图上不可查。要让规则变成**可被引用的对象**，只需要一条边：

    元素 --refers--> 规则

再配上本来就有的记忆链，整条路就通了：

    记忆 --has--> 元素 --refers--> 规则

三条规矩（都踩过坑，别改）
--------------------------
1. **只用 `refers` 一个谓词**。不用 `has`：`has/has:lead` 是 df 和骨干的唯一口径
   （grow.rebuild_df / grow.backbone 只数这两种），拿 `has` 挂规则会把规则算进
   文档频率、冲乱骨干元素的名次。
2. **指向生效版本，不指向旧版本**。规则层的约定是：锚点 `rule|<name>` 不动，
   另建版本节点 `rule|<name>|v<n>`，并连一条 `(锚点, 版本, 'current')` 当生效指针。
   所以这里**不 import 规则层**，只按边找：锚点有 `current` 边就指向它指的那版；
   没有（规则层还没建版本）才退回锚点。两种都处理，谁先谁后都能跑。
3. **匹配保守**。规则名与元素正文完全相等、或互为子串（子串长度 ≥2）才算数；
   宁可少连，不要乱连。连的时候把「为什么连」记下来（`auto_link()` 的 log）。

    python tools\\link_rule_elements.py                          # 只看会连哪些
    python tools\\link_rule_elements.py --db out/elem_rule.db --apply
"""
from __future__ import annotations

import sqlite3

from . import db

REFERS = "refers"          # 元素 → 规则（唯一新建的谓词）
CURRENT = "current"        # 规则层约定的生效指针：(锚点, 版本, 'current')
RULE_PREFIX = "rule|"
ELEMENT_KINDS = ("entity", "affect", "person")   # 与 flow.elements_in_order 同一口径
MIN_LEN = 2                # 单字不参与匹配：「边」「规」会误伤一片

RULE_COLUMNS = "id, kind, body, uniq"


# ── uniq 解析：名字 / 版本 ──────────────────────────────────────────────
def rule_name_of(uniq: str) -> str:
    """`rule|常驻取舍` 与 `rule|常驻取舍|v2` 都取 `常驻取舍`；不是规则返回 ''。"""
    parts = str(uniq or "").split("|")
    if len(parts) < 2 or parts[0] != "rule":
        return ""
    return parts[1].strip()


def version_of(uniq: str) -> str:
    """`rule|常驻取舍|v2` → `v2`；锚点或非规则 → ''。"""
    parts = str(uniq or "").split("|")
    return parts[2].strip() if len(parts) >= 3 and parts[0] == "rule" else ""


def rule_uniques(con: sqlite3.Connection) -> list[str]:
    """图里所有规则名（只认锚点 `rule|<name>`，版本节点不算新规则）。"""
    out: list[str] = []
    for r in con.execute("SELECT uniq FROM nodes WHERE kind='rule' ORDER BY id"):
        parts = str(r["uniq"]).split("|")
        if len(parts) == 2 and parts[0] == "rule" and parts[1].strip():
            out.append(parts[1].strip())
    return out


# ── 找点 / 找生效版本 ───────────────────────────────────────────────────
def _node(con: sqlite3.Connection, node_id: int) -> sqlite3.Row | None:
    return con.execute(f"SELECT {RULE_COLUMNS} FROM nodes WHERE id=?", (int(node_id),)).fetchone()


def effective_target(con: sqlite3.Connection, rule_node: int) -> tuple[int, str]:
    """返回 (要指向的节点, 'version'|'anchor')。

    传锚点时：有 current 边就指向它指的版本节点，没有就指向锚点自己。
    传版本节点时：就是它自己。
    """
    row = _node(con, rule_node)
    if not row or str(row["kind"]) != "rule":
        raise ValueError(f"不是规则节点：id={rule_node}")
    uniq = str(row["uniq"])
    if version_of(uniq):
        return int(row["id"]), "version"
    cur = con.execute(
        "SELECT dst FROM edges WHERE src=? AND rel=? ORDER BY id LIMIT 1",
        (int(row["id"]), CURRENT)).fetchone()
    if cur:
        ver = _node(con, int(cur["dst"]))
        if ver and str(ver["kind"]) == "rule":
            return int(ver["id"]), "version"
    return int(row["id"]), "anchor"


def resolve(con: sqlite3.Connection, rule_name: str) -> dict:
    """按名字（`常驻取舍` / `rule|常驻取舍` / `rule|常驻取舍|v1`）找到规则。"""
    raw = str(rule_name or "").strip()
    if raw.startswith(RULE_PREFIX):
        raw = raw[len(RULE_PREFIX):]
    parts = raw.split("|")
    name = parts[0].strip()
    version = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        raise LookupError("规则名为空")
    anchor_uniq = f"{RULE_PREFIX}{name}"
    anchor = db.get_node(con, anchor_uniq)
    if not anchor:
        raise LookupError(f"图里没有规则：{anchor_uniq}")
    if version:
        ver_uniq = f"{anchor_uniq}|{version}"
        ver = db.get_node(con, ver_uniq)
        if not ver:
            raise LookupError(f"图里没有这个规则版本：{ver_uniq}")
        target, target_kind = int(ver["id"]), "version"
    else:
        target, target_kind = effective_target(con, int(anchor["id"]))
    trow = _node(con, target)
    return {"name": name, "anchor": int(anchor["id"]),
            "anchor_uniq": anchor_uniq,
            "target": target, "target_kind": target_kind,
            "target_uniq": str(trow["uniq"]) if trow else ""}


# ── 建边 ───────────────────────────────────────────────────────────────
def link_element(con: sqlite3.Connection, elem_id: int, rule_name: str,
                 weight_hint: str = "") -> dict:
    """建一条 `元素 --refers--> 规则`。已存在就跳过（只增不改，不删任何边）。

    `weight_hint` 只是**调用方给的说明**：`edges` 没有属性列，权重按仓库一贯的
    做法「不存，算出来」，所以它只回传、不落库。

    返回 dict：added（这次真的新加了吗）/ element / rule / target / target_kind /
    target_uniq / weight_hint。
    """
    info = resolve(con, rule_name)
    erow = _node(con, elem_id)
    if not erow:
        raise LookupError(f"图里没有这个元素：id={elem_id}")
    if str(erow["kind"]) not in ELEMENT_KINDS:
        raise ValueError(
            f"id={elem_id} 是 kind={erow['kind']}，不是元素"
            f"（只收 {'/'.join(ELEMENT_KINDS)}）")
    if int(erow["id"]) == info["target"]:
        raise ValueError("元素和规则是同一个点，不能连")
    exists = con.execute(
        "SELECT 1 FROM edges WHERE src=? AND dst=? AND rel=?",
        (int(elem_id), info["target"], REFERS)).fetchone()
    if not exists:
        db.edge(con, int(elem_id), info["target"], REFERS)
        con.commit()
    return {"added": not bool(exists), "element": int(elem_id),
            "element_body": str(erow["body"]), "element_kind": str(erow["kind"]),
            "rule": info["name"], "target": info["target"],
            "target_kind": info["target_kind"], "target_uniq": info["target_uniq"],
            "weight_hint": str(weight_hint)}


# ── 查边 ───────────────────────────────────────────────────────────────
def _element_ids(con: sqlite3.Connection, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    marks = ",".join(str(int(x)) for x in ids)
    rows = con.execute(
        f"SELECT id FROM nodes WHERE id IN ({marks}) "
        f"AND kind IN ('entity','affect','person')").fetchall()
    return {int(r["id"]) for r in rows}


def refs_of_rule(con: sqlite3.Connection, rule_node: int) -> set[int]:
    """哪些元素指向这条规则。

    传锚点时把「指向锚点的」和「指向生效版本的」合起来算——旧图只连过锚点、
    后来才建版本，两种边都算这条规则的读者。传版本节点时只算那一版。
    """
    row = _node(con, rule_node)
    if not row or str(row["kind"]) != "rule":
        raise ValueError(f"不是规则节点：id={rule_node}")
    targets = {int(row["id"])}
    if not version_of(str(row["uniq"])):
        targets.add(effective_target(con, int(row["id"]))[0])
        for r in con.execute(
                "SELECT id FROM nodes WHERE kind='rule' AND uniq LIKE ?",
                (f"{row['uniq']}|%",)):
            targets.add(int(r["id"]))          # 旧版本也认（历史读者）
    src: set[int] = set()
    for t in targets:
        for r in con.execute(
                "SELECT DISTINCT src FROM edges WHERE dst=? AND rel=?", (t, REFERS)):
            src.add(int(r["src"]))
    return _element_ids(con, src)


def rules_of_element(con: sqlite3.Connection, elem_id: int) -> set[int]:
    """这个元素指向哪些规则（返回规则/版本节点 id）。

    **`refers` 与 `yields` 是同一关系的两个方向**（2026-11-20 实测确认）：
    一条规则产出某个元素，那个元素也就指认这条规则。所以查"元素碰得到哪些规则"时
    两头都要算，否则会出现"建了 124 条边、查询只看见 1 条"的错觉。

    一条元素被多于 `CAP` 条规则指向时会被截断——泛词（「代码」「记忆」）同时挂在
    好几条规则上，全留会把这条链变成噪声。
    """
    CAP = 2
    out: dict[int, int] = {}          # rule_node -> 命中次数（refers 权重更高）
    for r in con.execute(
            "SELECT DISTINCT dst FROM edges WHERE src=? AND rel=?", (int(elem_id), REFERS)).fetchall():
        n = _node(con, int(r["dst"]))
        if n and str(n["kind"]) == "rule":
            out[int(n["id"])] = 2                       # 直接指认：算两条
    for r in con.execute(
            "SELECT DISTINCT src FROM edges WHERE dst=? AND rel='yields'", (int(elem_id),)).fetchall():
        n = _node(con, int(r["src"]))
        if n and str(n["kind"]) == "rule":
            out.setdefault(int(n["id"]), 1)             # 规则产出它：算一条
    ranked = sorted(out.items(), key=lambda kv: -kv[1])[:CAP]
    return {k for k, _ in ranked}


def refs_by_rule(con: sqlite3.Connection) -> dict[str, set[int]]:
    """每条规则名 → 指向它的元素 id 集合（锚点口径）。"""
    out: dict[str, set[int]] = {}
    for name in rule_uniques(con):
        info = resolve(con, name)
        out[name] = refs_of_rule(con, info["anchor"])
    return out


def refers_edges(con: sqlite3.Connection) -> list[dict]:
    """图里现有全部 refers 边，带上两端正文，给 CLI 打印用。"""
    rows = con.execute(
        "SELECT e.src AS elem, e.dst AS rule, s.body AS elem_body, s.kind AS elem_kind, "
        "       d.uniq AS rule_uniq, d.body AS rule_body "
        "FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
        "WHERE e.rel=? ORDER BY e.id", (REFERS,)).fetchall()
    return [{"element": int(r["elem"]), "element_body": str(r["elem_body"]),
             "element_kind": str(r["elem_kind"]), "rule": int(r["rule"]),
             "rule_name": rule_name_of(str(r["rule_uniq"])),
             "rule_uniq": str(r["rule_uniq"]), "version": version_of(str(r["rule_uniq"])),
             "rule_body": str(r["rule_body"])} for r in rows]


# ── 自动匹配 ───────────────────────────────────────────────────────────
def match_level(elem_body: str, rule_name: str) -> tuple[str, str]:
    """规则名 ↔ 元素正文：完全相等 / 元素是规则名子串 / 规则名出现在元素里。

    返回 (level, why)；不匹配返回 ('', '')。子串必须 ≥2 字：
    单字（「边」「规」）在中文里几乎一定能对上，连上去就是乱连。
    """
    body = str(elem_body or "").strip()
    name = str(rule_name or "").strip()
    if len(body) < MIN_LEN or len(name) < MIN_LEN:
        return "", ""
    if body == name:
        return "exact", f"元素正文「{body}」与规则名完全相同"
    if body in name:
        return "substring", f"元素正文「{body}」是规则名「{name}」的子串（{len(body)}字）"
    if name in body:
        return "superset", f"规则名「{name}」出现在元素正文「{body}」里"
    return "", ""


def _elements(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, kind, body FROM nodes "
        "WHERE kind IN ('entity','affect','person') AND LENGTH(body) >= ? "
        "ORDER BY id", (MIN_LEN,)).fetchall()


def auto_link(con: sqlite3.Connection, dry_run: bool = True) -> dict:
    """按名字把元素连到规则上，只连高置信度的（完全相等或互为子串）。

    只对**锚点**（`rule|<name>`）做一轮：版本节点不是新规则，不能重复匹配。
    建边目标走 `effective_target()`：有生效版本连版本，没有连锚点。

    返回：dry_run / candidates（每条带 why）/ created / already / log。
    """
    rules: list[dict] = []
    for name in rule_uniques(con):
        rules.append(resolve(con, name))
    elems = _elements(con)

    candidates: list[dict] = []
    log: list[str] = []
    for info in rules:
        for e in elems:
            level, why = match_level(str(e["body"]), info["name"])
            if not level:
                continue
            candidates.append({
                "element": int(e["id"]), "element_body": str(e["body"]),
                "element_kind": str(e["kind"]), "rule": info["name"],
                "target": info["target"], "target_kind": info["target_kind"],
                "target_uniq": info["target_uniq"], "level": level,
                "why": why, "added": False,
            })

    created = already = 0
    for c in candidates:
        exists = con.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst=? AND rel=?",
            (c["element"], c["target"], REFERS)).fetchone()
        c["added"] = not bool(exists)
        if exists:
            already += 1
        else:
            created += 1
            log.append(f"连：{c['element_body']}({c['element_kind']}#{c['element']})"
                       f" --refers--> {c['target_uniq']}（{c['target_kind']}）｜{c['why']}")
            if not dry_run:
                db.edge(con, c["element"], c["target"], REFERS)
    if not dry_run:
        con.commit()

    for c in candidates:
        if not c["added"]:
            log.append(f"已有：{c['element_body']}(#{c['element']}) --refers--> "
                       f"{c['target_uniq']}｜{c['why']}")
    return {"dry_run": bool(dry_run), "rules": [r["name"] for r in rules],
            "n_elements": len(elems), "candidates": candidates,
            "created": 0 if dry_run else created,
            "will_create": created, "already": already, "log": log}


# ── 记忆 → 元素 → 规则 ─────────────────────────────────────────────────
def mem_elements(con: sqlite3.Connection, mem_id: int) -> list[sqlite3.Row]:
    """这条记忆挂着的元素（has / has:lead，和 flow._mems_with 同一口径）。"""
    return con.execute(
        "SELECT DISTINCT n.id, n.kind, n.body FROM edges e "
        "JOIN nodes n ON n.id=e.dst "
        "WHERE e.src=? AND e.rel IN ('has','has:lead') ORDER BY n.id",
        (int(mem_id),)).fetchall()


def explain(con: sqlite3.Connection, mem_id: int) -> dict:
    """一条记忆经哪些元素碰到哪些规则：`mem --has--> element --refers--> rule`。

    返回 {mem, text, elements:[{element, body, kind, rules:[{rule, rule_node,
    version, uniq}]}], rules:[规则名], n_edges}。
    元素一个不少地列出来；没连规则的元素 `rules` 是空列表——「没碰到」也要看得见。
    """
    row = con.execute("SELECT id, kind FROM nodes WHERE id=?", (int(mem_id),)).fetchone()
    if not row:
        raise LookupError(f"图里没有这个记忆：id={mem_id}")
    if str(row["kind"]) != "mem":
        raise ValueError(f"id={mem_id} 是 kind={row['kind']}，不是记忆")
    src = con.execute(
        "SELECT first_person, raw FROM mem_src WHERE mem_node=?", (int(mem_id),)).fetchone()
    text = str((src["first_person"] or src["raw"] or "") if src else "")

    elements: list[dict] = []
    names: list[str] = []
    n_edges = 0
    for e in mem_elements(con, int(mem_id)):
        rules = []
        for rid in sorted(rules_of_element(con, int(e["id"]))):
            rrow = _node(con, rid)
            if not rrow:
                continue
            rname = rule_name_of(str(rrow["uniq"]))
            rules.append({"rule": rname, "rule_node": int(rrow["id"]),
                          "version": version_of(str(rrow["uniq"])),
                          "uniq": str(rrow["uniq"])})
            n_edges += 1
            if rname not in names:
                names.append(rname)
        elements.append({"element": int(e["id"]), "body": str(e["body"]),
                         "kind": str(e["kind"]), "rules": rules})
    return {"mem": int(mem_id), "text": text, "elements": elements,
            "rules": names, "n_edges": n_edges}


def stats(con: sqlite3.Connection) -> dict:
    """给 CLI 用的总览：每条规则被几个元素指向、每个元素指向几条规则。"""
    by_rule = refs_by_rule(con)
    rules = []
    for name in rule_uniques(con):
        info = resolve(con, name)
        rules.append({"name": name, "anchor": info["anchor"],
                      "anchor_uniq": info["anchor_uniq"],
                      "target": info["target"], "target_kind": info["target_kind"],
                      "target_uniq": info["target_uniq"],
                      "n_elements": len(by_rule.get(name, set())),
                      "elements": sorted(by_rule.get(name, set()))})
    by_elem = {}
    for ed in refers_edges(con):
        by_elem.setdefault(ed["element"], {"element": ed["element"],
                                           "body": ed["element_body"],
                                           "kind": ed["element_kind"], "rules": []})
        by_elem[ed["element"]]["rules"].append(ed["rule_name"])
    elements = []
    for eid in sorted(by_elem):
        item = by_elem[eid]
        item["rules"] = sorted(set(item["rules"]))
        item["n_rules"] = len(item["rules"])
        elements.append(item)
    return {"rules": rules,
            "n_rules": len(rules),
            "n_elements_total": len(_elements(con)),
            "elements": elements,
            "n_refers": len(refers_edges(con))}
