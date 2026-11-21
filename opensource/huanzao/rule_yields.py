"""规则产出元素：`rule --yields--> element`，补齐「元素作唯一中介」的第三半。

整张图现在是这个形状：

    mem --has--> element        记忆挂着元素（df 与骨干只认这条）
    element --refers--> rule    元素指向规则（task-2 的 mg/rule_refs.py）
    rule --yields--> element    规则产出元素（本文件）

于是 M 与 R 之间只隔一个 X，元素是唯一的中介点：规则不只是「被元素指到」，
它自己也会**产出**元素——阈值名（`max_hops` / `innate` / `backbone` / `remind_chars`）、
词表里的词（「不入记忆」正文那 46 个工程词）、规则名里的实词（「取舍」「义项」「规矩」）。
这些元素与普通元素**同级**：同样 kind='entity'，同样可以被记忆引用（也能被 task-2 的
refers 连到其它规则上）。

三条护栏（写死在这，别改）
-------------------------
1. **只写 `yields` 一个谓词**。`has` / `has:lead` 是 df 与骨干的**唯一**口径
   （`grow.rebuild_df` / `grow.backbone` 只数这两种边，见 grow.py 的两条 SQL）。
   `yields` 天然不进这两个统计：df 只按 has 边的 dst 分组，骨干只从 has 边算覆盖。
   所以只要不碰 has 边，derived_df 的行数/合计、骨干元素集合都**一个数都不会变**。
   `tools/rule_yields_demo.py` 会逐项实测（还顺手把 df 与骨干按 has 边重算一遍对账）。
2. **绝不为了「让规则出现在召回里」给 yields 元素补 has 边。** 那是把规则正文里的词
   冒充成她想起来的事，篡改记忆的组成。yields 元素本来就可以是「已有 has 入边的元素」
   （例如「记忆」被 22 条记忆挂着）：我们只连 规则→元素，绝不动 记忆→元素。
3. **指向生效版本，不指向旧版本**（与 mg/rule_refs.py 同一约定）：锚点
   `rule|<name>` 有 `current` 边就连它指的版本节点 `rule|<name>|v<n>`，没有就连锚点。
   这里不 import 规则层，只按边找——谁先谁后都能跑。
4. **只增不改**：只建点、只加边；不删节点、不删边、不覆盖任何正文。重复跑是幂等的。

关键词从哪来（三层，都从**规则正文**取值，不只从规则名）
-------------------------------------------------------
* **键**：键值型正文里的 `k=v` 的键（`max_hops`、`min_hit_cov`、`innate`、`backbone`、
  `remind_chars`、`tail_tokens`、`margin`、`past`、`now`、`now_precision`、`title`、
  `footer`、`per_mem`）——它们本来就是可指认的东西；
* **值**：键值型正文里**短**的值（`叙事坐标`、`日历`）——长句/带括号/纯数字一律不收；
* **词表**：词表型正文里的词（「不入记忆」那 46 个工程词、「操作词」25 个、「话题词」24 个）；
* **名**：规则名里的实词。10 条规则名是**封闭集合**，怎么切词写死在 `NAME_WORDS` 里：
  中文没有现成分词器，与其写一个会切出「文预」「心校验」的通用算法，不如把这 10 行写死
  （「宁可少连，不要乱连」）。

只连「长度 ≥2、不是纯数字」的词；中文字收 2..6 字（与 `flow.ensure_elements` 同一口径），
ASCII 标识符（`max_hops` 这种）收 2..24 字符，因为它本来就是一个整词。
图里已经有同名节点的词**不建第二张皮**（`flow.ensure_elements` 的约定：按正文判重），
如果那个同名节点不是元素（kind 不是 entity/affect/person），就跳过、不连——
规则只能产出元素，不能把别的层的东西硬说成元素。

    python tools\\rule_yields_demo.py --db out/rule_yields.db            # dry-run
    python tools\\rule_yields_demo.py --db out/rule_yields.db --apply    # 真建边
"""
from __future__ import annotations

import re
import sqlite3

from . import db

YIELDS = "yields"            # 规则 → 元素（唯一新建的谓词）
CURRENT = "current"          # 规则层约定的生效指针：(锚点, 版本, 'current')
REFERS = "refers"            # 元素 → 规则（task-2 的谓词，这里只读进来做统计）
RULE_PREFIX = "rule|"
ELEMENT_KINDS = ("entity", "affect", "person")   # 与 flow.elements_in_order 同一口径
MEM_RELS = ("has", "has:lead")                   # df 与骨干的唯一口径（只读，不写）

MIN_LEN = 2                  # 单字不参与：「边」「规」「先」会误伤一片
MAX_CJK = 6                 # 中文字词上限（flow.ensure_elements 是 2..6）
MAX_ASCII = 24               # ASCII 标识符上限：min_hit_cov / remind_chars 是一个整词

_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_ASCII_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KV_KEY = re.compile(
    r"(?:^|[；\n])\s*(?P<k>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff\-]*)\s*=")
_BAD_CHARS = set("，。、；：（）【】「」『』《》〈〉／/\\|!?！？…·,.=+-*#%&@$^~`\"'<>[]{} \t\n\r：")

# ── 规则名 → 实词：10 条规则名是封闭集合，切词写死在这（不写会切出半截词的通用分词） ──
# 「不入」不是词，只取「记忆」；「操作词／话题词」的「词」是后缀，取「操作／话题」。
NAME_WORDS: dict[str, list[str]] = {
    "操作词": ["操作"],
    "话题词": ["话题"],
    "中心校验": ["中心", "校验"],
    "义项裁定": ["义项", "裁定"],
    "规矩": ["规矩"],
    "提醒措辞": ["提醒", "措辞"],
    "常驻取舍": ["常驻", "取舍"],
    "时间坐标": ["时间", "坐标"],
    "上下文预算": ["上下文", "预算"],
    "不入记忆": ["记忆"],
}


# ══════════════════════════════════════════════════════════════════
# 一、词的清洗与规则的寻址
# ══════════════════════════════════════════════════════════════════

def word_ok(word: str) -> bool:
    """这个词能不能当元素：长度 ≥2、非纯数字、不带标点。

    中文/混合词收 2..6 字；ASCII 标识符（`max_hops`、`sqlite`）收 2..24 字符。
    """
    w = str(word or "").strip()
    if len(w) < MIN_LEN or _NUMBER.match(w):
        return False
    if any(ch in _BAD_CHARS for ch in w):
        return False
    if _ASCII_ID.match(w):
        return len(w) <= MAX_ASCII
    return len(w) <= MAX_CJK and not any(ch.isdigit() for ch in w)


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


def rule_names(con: sqlite3.Connection) -> list[str]:
    """图里所有规则名（只认锚点 `rule|<name>`，版本节点不算新规则）。"""
    out: list[str] = []
    for r in con.execute("SELECT uniq FROM nodes WHERE kind='rule' ORDER BY id"):
        parts = str(r["uniq"]).split("|")
        if len(parts) == 2 and parts[0] == "rule" and parts[1].strip():
            out.append(parts[1].strip())
    return out


def _node(con: sqlite3.Connection, node_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, kind, body, uniq FROM nodes WHERE id=?", (int(node_id),)).fetchone()


def effective_target(con: sqlite3.Connection, rule_node: int) -> tuple[int, str]:
    """返回 (要连的节点, 'version'|'anchor')。与 mg/rule_refs.effective_target 同一约定。

    传锚点时：有 current 边就连它指的版本节点，没有就连锚点自己。传版本节点时就是它。
    """
    row = _node(con, rule_node)
    if not row or str(row["kind"]) != "rule":
        raise ValueError(f"不是规则节点：id={rule_node}")
    if version_of(str(row["uniq"])):
        return int(row["id"]), "version"
    cur = con.execute(
        "SELECT dst FROM edges WHERE src=? AND rel=? ORDER BY id LIMIT 1",
        (int(row["id"]), CURRENT)).fetchone()
    if cur:
        ver = _node(con, int(cur["dst"]))
        if ver and str(ver["kind"]) == "rule":
            return int(ver["id"]), "version"
    return int(row["id"]), "anchor"


def resolve_rule(con: sqlite3.Connection, rule_name: str) -> dict:
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
        ver = db.get_node(con, f"{anchor_uniq}|{version}")
        if not ver:
            raise LookupError(f"图里没有这个规则版本：{anchor_uniq}|{version}")
        target, target_kind = int(ver["id"]), "version"
    else:
        target, target_kind = effective_target(con, int(anchor["id"]))
    trow = _node(con, target)
    return {"name": name, "anchor": int(anchor["id"]), "anchor_uniq": anchor_uniq,
            "target": target, "target_kind": target_kind,
            "target_uniq": str(trow["uniq"]) if trow else "",
            "body": str(trow["body"]) if trow else ""}


# ══════════════════════════════════════════════════════════════════
# 二、从规则正文里取词
# ══════════════════════════════════════════════════════════════════

def _kv_pairs(tail: str) -> list[tuple[str, str]]:
    """把 `k=v；k2=v2` 切成 [(k, v)]；值里的「；」不会被切断（切在下一个「键=」上）。"""
    ms = list(_KV_KEY.finditer(tail))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(ms):
        start = m.end()
        stop = ms[i + 1].start() if i + 1 < len(ms) else len(tail)
        val = tail[start:stop].strip().strip("；").strip()
        out.append((str(m.group("k")).strip(), val))
    return out


def keywords_of_rule(rule_name: str, body: str) -> list[dict]:
    """一条规则产出的关键词：`[{word, source}]`，source ∈ 键/值/词表/名。

    按正文出现顺序排；同一个词只留第一次出现（来源记第一次那个）。
    """
    title, sep, tail = str(body or "").strip().partition("：")
    found: list[tuple[str, str]] = []
    if sep and tail.strip():
        if "=" in tail:                                  # 键值型
            for k, v in _kv_pairs(tail):
                found.append((k, "键"))
                if word_ok(v):
                    found.append((v, "值"))
        else:                                            # 词表型
            for it in re.split(r"[、／]", tail):
                found.append((it.strip(), "词表"))
    for w in NAME_WORDS.get(str(rule_name).strip(),
                            [str(rule_name).strip()]):    # 名：封闭表 + 兜底取整名
        found.append((w, "名"))

    out: list[dict] = []
    seen: set[str] = set()
    for word, source in found:
        w = str(word or "").strip()
        if not word_ok(w) or w in seen:
            continue
        seen.add(w)
        out.append({"word": w, "source": source})
    return out


# ══════════════════════════════════════════════════════════════════
# 三、找元素 / 建边
# ══════════════════════════════════════════════════════════════════

def _find_element(con: sqlite3.Connection, body: str) -> tuple[int | None, str, bool]:
    """按正文找元素。返回 (id, kind, 需要新建吗)。

    * 有 `entity|<body>` 就用它；
    * 没有的话，任何 entity/affect/person 同正文的节点都认（`person|丁` 与
      `entity|丁` 是两张皮，yields 不挑皮，认第一张）；
    * 正文被非元素节点占着（kind='day'/'thought' 之类）→ 返回 (None, 那个kind, False)，
      调用方跳过：按 flow.ensure_elements 的约定不建第二张皮，也不能把非元素说成元素；
    * 正文没人用 → (None, '', True)，该建 `entity|<body>`。
    """
    rows = con.execute(
        "SELECT id, kind, uniq FROM nodes WHERE body=? ORDER BY id", (str(body),)).fetchall()
    for r in rows:
        if str(r["kind"]) in ELEMENT_KINDS and str(r["uniq"]) == f"entity|{body}":
            return int(r["id"]), str(r["kind"]), False
    for r in rows:
        if str(r["kind"]) in ELEMENT_KINDS:
            return int(r["id"]), str(r["kind"]), False
    if rows:
        return None, str(rows[0]["kind"]), False
    return None, "", True


def _yield_exists(con: sqlite3.Connection, rule_target: int, element: int) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM edges WHERE src=? AND dst=? AND rel=?",
        (int(rule_target), int(element), YIELDS)).fetchone())


def link_yield(con: sqlite3.Connection, rule_name: str, element_body: str) -> dict:
    """建一条 `rule --yields--> element`；元素不存在就按 flow 的约定建 entity。

    只增不改：已存在的边跳过，已存在的元素复用，不删任何东西。
    返回 {added, rule, anchor_uniq, target, target_kind, target_uniq,
          element, element_body, element_kind, element_created}。
    """
    info = resolve_rule(con, rule_name)
    body = str(element_body or "").strip()
    if not word_ok(body):
        raise ValueError(f"「{body}」不能当元素：长度要 ≥2、非纯数字、不带标点")
    elem_id, elem_kind, need_create = _find_element(con, body)
    created = False
    if elem_id is None and not need_create:
        raise ValueError(f"「{body}」在图里是 {elem_kind} 节点，不是元素")
    if elem_id is None:
        elem_id = db.node(con, "entity", body, uniq=f"entity|{body}")
        created = True
    if elem_id == info["target"]:
        raise ValueError("元素和规则是同一个点，不能连")
    added = not _yield_exists(con, int(info["target"]), int(elem_id))
    if added:
        db.edge(con, int(info["target"]), int(elem_id), YIELDS)
    con.commit()
    return {"added": added, "rule": info["name"], "anchor_uniq": info["anchor_uniq"],
            "target": info["target"], "target_kind": info["target_kind"],
            "target_uniq": info["target_uniq"], "element": int(elem_id),
            "element_body": body, "element_kind": elem_kind or "entity",
            "element_created": created}


# ══════════════════════════════════════════════════════════════════
# 四、seed：为 10 条规则各抽出关键词元素并连边
# ══════════════════════════════════════════════════════════════════

def seed_yields(con: sqlite3.Connection, dry_run: bool = True) -> dict:
    """给每条规则抽关键词元素，连 `rule --yields--> element`。

    dry_run=True 时**不写库**（不建点、不建边），只把计划算出来；
    dry_run=False 与 dry-run 走同一份计划。幂等：重复跑新增为 0。

    返回 {dry_run, rules: [...], totals, log}；每条规则带 words（词+来源+状态）。
    """
    rules: list[dict] = []
    for name in rule_names(con):
        info = resolve_rule(con, name)
        words = keywords_of_rule(name, info["body"])
        plans: list[dict] = []
        for kw in words:
            w = kw["word"]
            elem_id, elem_kind, need_create = _find_element(con, w)
            p = {"word": w, "source": kw["source"], "element": elem_id,
                 "element_kind": elem_kind, "will_create": need_create,
                 "created": False, "added": False, "planned": False, "status": ""}
            if elem_id is None and not need_create:
                p["status"] = f"跳过：图里同名正文是 {elem_kind} 节点，不是元素"
                plans.append(p)
                continue
            if need_create:                       # 元素还没有（dry-run 只标记「要建」）
                p["planned"] = True
                p["status"] = "新建元素+边（dry-run 未写）" if dry_run else "新建元素+边"
            else:
                exists = _yield_exists(con, int(info["target"]), int(elem_id))
                p["added"] = not exists
                p["planned"] = not exists
                p["status"] = "已有边" if exists else "已有元素，新建边"
            plans.append(p)
        rules.append({"name": name, "anchor": info["anchor"],
                      "target": info["target"], "target_kind": info["target_kind"],
                      "target_uniq": info["target_uniq"], "body": info["body"],
                      "words": plans})

    if not dry_run:
        for r in rules:
            for p in r["words"]:
                if p["status"].startswith("跳过"):
                    continue
                res = link_yield(con, r["name"], p["word"])
                p.update({"element": res["element"], "element_kind": res["element_kind"],
                          "created": res["element_created"], "added": res["added"],
                          "planned": False,
                          "status": "新建元素+边" if res["element_created"] else
                                    ("新建边" if res["added"] else "已有边")})

    log: list[str] = []
    for r in rules:
        for p in r["words"]:
            if p["status"].startswith("跳过"):
                log.append(f"跳过：{r['name']} →「{p['word']}」（{p['status'][3:]}）")
            elif p["added"] or (dry_run and p["planned"]):
                log.append(f"连：{r['target_uniq']} --yields--> "
                           f"「{p['word']}」({p['element_kind']}#{p['element']})"
                           f"｜来源={p['source']}"
                           + ("｜元素是新建的" if p["created"] or
                              (dry_run and p["will_create"]) else "｜元素已存在"))
            else:
                log.append(f"已有：{r['target_uniq']} --yields--> 「{p['word']}」"
                           f"(#{p['element']})")

    planned = [p for r in rules for p in r["words"] if not p["status"].startswith("跳过")]
    if dry_run:
        # 同一个词可能被两条规则同时产出：dry-run 按「词」去重算要建几个元素
        new_nodes = len({p["word"] for p in planned if p["will_create"]})
        new_edges = sum(1 for p in planned if p["planned"])
        already_edges = sum(1 for p in planned if p["element"] is not None and not p["planned"])
    else:
        new_nodes = sum(1 for p in planned if p["created"])
        new_edges = sum(1 for p in planned if p["added"])
        already_edges = sum(1 for p in planned if not p["added"])
    totals = {
        "rules": len(rules),
        "words": len(planned),
        "skipped": sum(1 for r in rules for p in r["words"]
                       if p["status"].startswith("跳过")),
        "new_edges": new_edges,
        "already_edges": already_edges,
        "new_nodes": new_nodes,
        "dry_run": bool(dry_run),
    }
    return {"dry_run": bool(dry_run), "rules": rules, "totals": totals, "log": log}


# ══════════════════════════════════════════════════════════════════
# 五、查边 / 统计 / 不变量
# ══════════════════════════════════════════════════════════════════

def _element_ids(con: sqlite3.Connection, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    marks = ",".join(str(int(x)) for x in ids)
    rows = con.execute(
        f"SELECT id FROM nodes WHERE id IN ({marks}) "
        f"AND kind IN ('entity','affect','person')").fetchall()
    return {int(r["id"]) for r in rows}


def yields_of_rule(con: sqlite3.Connection, rule_node: int) -> set[int]:
    """这条规则产出了哪些元素。

    传锚点时把「从锚点连出去的」和「从生效版本、历史版本连出去的」合起来算——
    规则升级后旧 yields 边仍然记着这条规则产出过什么。传版本节点时只算那一版。
    """
    row = _node(con, rule_node)
    if not row or str(row["kind"]) != "rule":
        raise ValueError(f"不是规则节点：id={rule_node}")
    srcs = {int(row["id"])}
    if not version_of(str(row["uniq"])):
        srcs.add(effective_target(con, int(row["id"]))[0])
        for r in con.execute(
                "SELECT id FROM nodes WHERE kind='rule' AND uniq LIKE ?",
                (f"{row['uniq']}|%",)):
            srcs.add(int(r["id"]))
    dst: set[int] = set()
    for s in srcs:
        for r in con.execute(
                "SELECT DISTINCT dst FROM edges WHERE src=? AND rel=?", (s, YIELDS)):
            dst.add(int(r["dst"]))
    return _element_ids(con, dst)


def yields_edges(con: sqlite3.Connection) -> list[dict]:
    """图里现有全部 yields 边，带两端正文，给 CLI 打印用。"""
    rows = con.execute(
        "SELECT e.src AS rule, e.dst AS elem, s.uniq AS rule_uniq, s.body AS rule_body, "
        "       d.body AS elem_body, d.kind AS elem_kind "
        "FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
        "WHERE e.rel=? ORDER BY e.id", (YIELDS,)).fetchall()
    return [{"rule": int(r["rule"]), "rule_name": rule_name_of(str(r["rule_uniq"])),
             "rule_uniq": str(r["rule_uniq"]), "version": version_of(str(r["rule_uniq"])),
             "element": int(r["elem"]), "element_body": str(r["elem_body"]),
             "element_kind": str(r["elem_kind"])} for r in rows]


def stats(con: sqlite3.Connection) -> dict:
    """每条规则产出几个元素、产出的是哪些；以及「同时被记忆和规则引用」的元素。"""
    by_rule = {name: yields_of_rule(con, resolve_rule(con, name)["anchor"])
               for name in rule_names(con)}
    rules = []
    for name in rule_names(con):
        info = resolve_rule(con, name)
        ids = by_rule.get(name, set())
        els = []
        for eid in sorted(ids):
            row = _node(con, eid)
            if not row:
                continue
            els.append({"element": eid, "body": str(row["body"]),
                        "kind": str(row["kind"]),
                        "has_in": _has_in(con, eid),
                        "refers_out": _refers_out(con, eid)})
        rules.append({"name": name, "anchor": info["anchor"],
                      "anchor_uniq": info["anchor_uniq"], "target": info["target"],
                      "target_kind": info["target_kind"],
                      "target_uniq": info["target_uniq"],
                      "n_elements": len(els), "elements": els})

    # 同一元素既有 has 入边（记忆挂着它）又有 yields 入边（规则产出它）
    dual: list[dict] = []
    seen_elem: set[int] = set()
    for ed in yields_edges(con):
        eid = ed["element"]
        if eid in seen_elem:
            continue
        seen_elem.add(eid)
        has_in = _has_in(con, eid)
        if has_in <= 0:
            continue
        row = _node(con, eid)
        producers = [x["rule_name"] for x in yields_edges(con) if x["element"] == eid]
        dual.append({"element": eid, "body": str(row["body"]) if row else "",
                     "kind": str(row["kind"]) if row else "", "has_in": has_in,
                     "yields_in": len(producers),
                     "rules": sorted(set(producers))})
    return {"rules": rules, "n_rules": len(rules),
            "n_yields": len(yields_edges(con)),
            "n_elements_with_yields": len(seen_elem),
            "dual": dual}


def _has_in(con: sqlite3.Connection, elem_id: int) -> int:
    return int(con.execute(
        "SELECT COUNT(*) c FROM edges WHERE dst=? AND rel IN (?,?)",
        (int(elem_id),) + MEM_RELS).fetchone()["c"])


def _refers_out(con: sqlite3.Connection, elem_id: int) -> int:
    return int(con.execute(
        "SELECT COUNT(*) c FROM edges WHERE src=? AND rel=?", (int(elem_id), REFERS)
    ).fetchone()["c"])


def invariants(con: sqlite3.Connection) -> dict:
    """建边前后必须**一模一样**的口径（yields 边自身除外）。"""
    one = lambda sql: int(con.execute(sql).fetchone()[0])  # noqa: E731
    return {
        "nodes": one("SELECT COUNT(*) FROM nodes"),
        "mem": one("SELECT COUNT(*) FROM nodes WHERE kind='mem'"),
        "rule": one("SELECT COUNT(*) FROM nodes WHERE kind='rule'"),
        "entity": one("SELECT COUNT(*) FROM nodes WHERE kind='entity'"),
        "derived_df_rows": one("SELECT COUNT(*) FROM derived_df"),
        "derived_df_sum": one("SELECT COALESCE(SUM(df),0) FROM derived_df"),
        "has_edges": one("SELECT COUNT(*) FROM edges WHERE rel='has'"),
        "has_lead_edges": one("SELECT COUNT(*) FROM edges WHERE rel='has:lead'"),
        "backbone_edges": one("SELECT COUNT(*) FROM edges WHERE rel='backbone'"),
        "refers_edges": one("SELECT COUNT(*) FROM edges WHERE rel='refers'"),
        "yields_edges": one("SELECT COUNT(*) FROM edges WHERE rel='yields'"),
        "all_edges": one("SELECT COUNT(*) FROM edges"),
    }


def df_from_has(con: sqlite3.Connection) -> dict[int, int]:
    """按 has/has:lead 边重算 df（只读，不写 derived_df）——用来证明 df 只由 has 决定。"""
    return {int(r["dst"]): int(r["df"]) for r in con.execute(
        "SELECT dst, COUNT(DISTINCT src) df FROM edges "
        "WHERE rel IN ('has','has:lead') GROUP BY dst")}
