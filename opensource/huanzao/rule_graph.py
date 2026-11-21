"""规则层（档 2 自指的 R^soft 那一半）：版本链、极性账本、带符号环检测、外部审计。

三条固定约定（任务书定死，三条并行线共用，谁都不许改）
------------------------------------------------------
1. 版本节点：uniq = ``rule|<name>|v<n>``，kind='rule'，body = 该版正文。
   旧版本**永不删除、不覆盖**（A3 只增不改）。
2. 生效指针：锚点 ``rule|<name>``（无版本后缀）的 body 保持原样；
   指针用边 ``(anchor, version, 'current')`` 表示。
   读取：有 current 边读它指向的版本，否则读锚点 body。
   切指针 = 原地改那条 current 边的 dst（只改这一行的 dst，不新增、不删除任何行）。
   为什么不是「每次切都新增一条 current 边」：约定要求「若存在 current 边则读它指向的
   版本」——这句话要成立，生效指针就得唯一。而 A3 又不许删行，两者只能这样调和。
   变更史一点没丢：每次写都留一条 kind='rulelog' 的日志节点 + 一条带符号改写边。
3. 极性：由原子操作类型唯一决定（定义 8），**不人工标注**，也不存边属性——
   edges 表只有 id/src/dst/rel 三列，符号就存在谓词里：

       merge / stop- / thr↑ / split = +1  →  rel = 'rewrites'
       stop+ / thr↓                = -1  →  rel = 'unrewrites'

带符号图（定义 9 的 groupoid 投影到规则图上）
--------------------------------------------
* 点在带符号改写边上：版本节点（都属于 kind='rule'）。
* 边 ``v_old -> v_new``，符号 = 产生该版本的原子操作极性；逆操作（revert）补一条反向边，
  符号取反——这就是定理 7「逆操作必反相」，也是定理 8 里 L1（单圈，极性 -1）的自动来源。
* 环极性 = 路径上谓词符号之积，取值 ±1（Z/2）。``cycles()`` 枚举长度 2..max_len 的简单环。

外部一致性检查器
----------------
``audit()`` **不经过写入路径**：自己发 SQL 读 nodes / edges，自己解析正文，检查
① 同名多 current 指针 ② current 指向不存在的节点 ③ body 解析失败 ④ 阈值越界。
它只读，不改图。对应 v0.2 边界 3：「规则集一致性不能由自指保证，需要独立的外部冲突检测」。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import db

# ── 约定里的字符串常量：只在这里出现一次 ──
ANCHOR_PREFIX = "rule|"
LOG_PREFIX = "rulelog|"
VERSION_RE = re.compile(r"^(?P<name>.*)\|v(?P<n>\d+)$")

REL_POS = "rewrites"      # 极性 +1
REL_NEG = "unrewrites"    # 极性 -1

# ── 定义 8：极性由操作类型唯一决定 ──
OP_POLARITY: dict[str, int] = {
    "merge": +1,     # a 的实例并入 b，b 的适用集扩大
    "stop+": -1,     # 把 w 拉进停用表，可作种子的输入减少
    "stop-": +1,     # 移出停用表
    "thr↑": +1,      # 阈值放宽（数值变大）
    "thr↓": -1,      # 阈值收紧（数值变小）
    "split": +1,     # 一义拆二，适用集并集不小于原集
}
# 命令行里敲不出 ↑↓ 时用 ASCII 别名（CLI 只做归一化，不改符号定义）
OP_ALIASES = {
    "merge": "merge", "合并": "merge",
    "stop+": "stop+", "stop_plus": "stop+", "stop-": "stop-", "stop_minus": "stop-",
    "thr↑": "thr↑", "thr_up": "thr↑", "thr+": "thr↑",
    "thr↓": "thr↓", "thr_down": "thr↓", "thr-": "thr↓",
    "split": "split", "拆分": "split",
}
MANUAL_OPS = {"manual", "人工", "手改"}   # 非原子改写：记账，但不进极性账本（极性无法判定）

# ── 审计用阈值上下界（对应 rules.py 里的 DEFAULT_*） ──
RULE_BOUNDS: dict[str, tuple[float, float]] = {
    "innate": (0, 200),
    "backbone": (0, 2000),
    "remind_chars": (50, 20000),
    "tail_tokens": (16, 200000),
    "per_mem": (10, 5000),
    "max_hops": (1, 32),
    "min_hit_cov": (0.0, 1.0),
    "margin": (0.0, 1.0),
}


class RuleError(ValueError):
    """规则层拒绝写入时抛这个（不猜、不静默改）。"""


# ══════════════════════════════════════════════════════════════════
# 一、名字与节点寻址
# ══════════════════════════════════════════════════════════════════

def _normalize_name(name: str) -> str:
    """把 'rule|常驻取舍'、'常驻取舍' 归一到 '常驻取舍'；版本名不许当规则名用。"""
    n = (name or "").strip()
    if n.startswith(ANCHOR_PREFIX):
        n = n[len(ANCHOR_PREFIX):]
    if not n:
        raise RuleError("规则名为空")
    if VERSION_RE.match(n):
        raise RuleError(f"{name!r} 是版本节点，不能当规则名（版本不可再分版本）")
    return n


def _anchor_uniq(name: str) -> str:
    return f"{ANCHOR_PREFIX}{name}"


def _version_uniq(name: str, n: int) -> str:
    return f"{ANCHOR_PREFIX}{name}|v{int(n)}"


def _rule_name(uniq: str) -> str:
    """从节点 uniq 取规则名：rule|X|v3 -> X，rule|X -> X。"""
    s = (uniq or "")
    if s.startswith(ANCHOR_PREFIX):
        s = s[len(ANCHOR_PREFIX):]
    m = VERSION_RE.match(s)
    return m.group("name") if m else s


def _version_of_uniq(uniq: str) -> int | None:
    m = VERSION_RE.match((uniq or "")[len(ANCHOR_PREFIX):] if (uniq or "").startswith(ANCHOR_PREFIX) else (uniq or ""))
    return int(m.group("n")) if m else None


def _node(con: sqlite3.Connection, uniq: str) -> sqlite3.Row | None:
    return db.get_node(con, uniq)


def _body_of(con: sqlite3.Connection, node_id: int) -> str:
    row = con.execute("SELECT body FROM nodes WHERE id=?", (int(node_id),)).fetchone()
    return str(row["body"]) if row else ""


def _rule_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute("SELECT id, kind, body, uniq FROM nodes WHERE kind='rule' ORDER BY id"))


def _version_numbers(con: sqlite3.Connection, name: str) -> list[int]:
    nums = []
    for row in _rule_rows(con):
        m = VERSION_RE.match(str(row["uniq"])[len(ANCHOR_PREFIX):]) if str(row["uniq"]).startswith(ANCHOR_PREFIX) else None
        if m and m.group("name") == name:
            nums.append(int(m.group("n")))
    return sorted(nums)


def _next_version_number(con: sqlite3.Connection, name: str) -> int:
    nums = _version_numbers(con, name)
    return (nums[-1] + 1) if nums else 1


def _current_id(con: sqlite3.Connection, name: str) -> int | None:
    """生效指针指向的节点 id（最多一条；有多条时取最新那条，多指针本身由 audit 报）。"""
    anchor = _node(con, _anchor_uniq(name))
    if anchor is None:
        return None
    row = con.execute(
        "SELECT dst FROM edges WHERE src=? AND rel='current' ORDER BY id DESC LIMIT 1",
        (int(anchor["id"]),),
    ).fetchone()
    return int(row["dst"]) if row else None


# ══════════════════════════════════════════════════════════════════
# 二、正文解析（规则正文只有两种形状：词表 / 键值）
# ══════════════════════════════════════════════════════════════════

_KV_KEY = re.compile(r"(?:^|；|\n)\s*(?P<k>[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff\-]*)\s*=")


def parse_body(body: str) -> dict:
    """解析规则正文。

    形状一（词表）：``标题：词1、词2``（或 ``标题：句1／句2``）
    形状二（键值）：``标题：k=v；k2=v2``

    返回 ``{title, form, kvs, items, sep}``；解析不了就抛 RuleError——
    审计的第③项就是拿它跑一遍全部规则节点。
    """
    text = (body or "").strip()
    if not text:
        raise RuleError("正文为空")
    title, sep, tail = text.partition("：")
    title, tail = title.strip(), tail.strip()
    if not sep:
        raise RuleError("正文里没有「：」，分不出标题与内容")
    if not title:
        raise RuleError("标题为空")
    if not tail:
        raise RuleError("「：」后面没有内容")

    if "=" in tail:
        matches = list(_KV_KEY.finditer(tail))
        if not matches:
            raise RuleError("正文含「=」却找不到「键=」，不是合法的键值型正文")
        head = tail[: matches[0].start()].strip().strip("；")
        if head:
            raise RuleError(f"第一个「键=」前面有多余内容：{head!r}")
        kvs: list[tuple[str, str]] = []
        for i, m in enumerate(matches):
            start = m.end()
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(tail)
            raw = tail[start:stop].strip().strip("；").strip()
            if not raw:
                raise RuleError(f"键 {m.group('k')!r} 没有值")
            kvs.append((m.group("k"), raw))
        return {"title": title, "form": "kv", "kvs": kvs, "items": [], "sep": "；", "tail": tail}

    sep_char = "／" if ("／" in tail and "、" not in tail) else "、"
    items = [x.strip() for x in tail.split(sep_char) if x.strip()]
    if not items:
        raise RuleError("词表型正文没有条目")
    for it in items:
        if "=" in it:
            raise RuleError(f"词表条目里带「=」：{it!r}（键值形态请统一成 k=v）")
    return {"title": title, "form": "list", "kvs": [], "items": items, "sep": sep_char, "tail": tail}


def _fmt_kv(title: str, kvs: list[tuple[str, str]]) -> str:
    return f"{title}：" + "；".join(f"{k}={v}" for k, v in kvs)


def _fmt_list(title: str, items: list[str], sep: str) -> str:
    return f"{title}：" + sep.join(items)


def _as_number(raw: str) -> int | float:
    try:
        return float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
    except ValueError as exc:
        raise RuleError(f"不是数字：{raw!r}") from exc


# ══════════════════════════════════════════════════════════════════
# 三、写：版本 + 指针 + 变更日志
# ══════════════════════════════════════════════════════════════════

def _log(con: sqlite3.Connection, name: str, ver: str, old: str, new: str,
         op: str, by: str, why: str, polarity: int | None) -> int:
    """一条变更日志就是一个 kind='rulelog' 的节点（谁/为什么/旧值→新值）。"""
    when = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    pol = "无" if polarity is None else f"{polarity:+d}"
    body = (f"{when} {ver} ← 前态 ｜ by={by or '-'} ｜ op={op} 极性={pol} ｜ "
            f"why={why or '-'} ｜ 旧：{old or '-'} ｜ 新：{new or '-'}")
    uniq = f"{LOG_PREFIX}{name}|{ver}"
    lid = db.node(con, "rulelog", body, uniq=uniq)
    anchor = _node(con, _anchor_uniq(name))
    if anchor is not None:
        db.edge(con, lid, int(anchor["id"]), "log-for")
    n = _version_of_uniq(f"{ANCHOR_PREFIX}{name}|{ver}") if ver.startswith("v") else _version_of_uniq(ver)
    if n is not None:
        ver_node = _node(con, _version_uniq(name, n))
        if ver_node is not None:
            db.edge(con, lid, int(ver_node["id"]), "log-of")
    return lid


def _log_info(con: sqlite3.Connection, name: str, ver: str) -> dict:
    """从日志节点正文里取 by / why / op / 极性（给 history 显示用）。"""
    row = _node(con, f"{LOG_PREFIX}{name}|{ver}")
    if row is None:
        return {}
    parts = str(row["body"]).split("｜")
    info: dict = {}
    for p in parts[1:]:
        key, _, val = p.strip().partition("=")
        if key in ("by", "why"):
            info[key] = val.strip()
        elif key.startswith("op"):
            info["op"] = val.strip()
    m = re.search(r"op=(?P<op>\S+) 极性=(?P<pol>\S+)", str(row["body"]))
    if m:
        info["op"] = m.group("op")
        pol = m.group("pol")
        info["polarity"] = 0 if pol == "无" else int(pol)
    return info


def _point_current(con: sqlite3.Connection, name: str, ver_id: int) -> None:
    """切生效指针：原地改那条 current 边的 dst；没有就新增一条。不删任何行。"""
    anchor = _node(con, _anchor_uniq(name))
    if anchor is None:
        raise RuleError(f"锚点不存在：{_anchor_uniq(name)}")
    row = con.execute(
        "SELECT id FROM edges WHERE src=? AND rel='current' ORDER BY id DESC LIMIT 1",
        (int(anchor["id"]),),
    ).fetchone()
    if row:
        con.execute("UPDATE edges SET dst=? WHERE id=?", (int(ver_id), int(row["id"])))
    else:
        con.execute("INSERT INTO edges(src, dst, rel) VALUES(?,?, 'current')",
                    (int(anchor["id"]), int(ver_id)))


def _canon_op(op: str, *, allow_manual: bool = False) -> str:
    raw = (op or "").strip()
    for cand in (raw, raw.lower()):
        if cand in OP_ALIASES:
            return OP_ALIASES[cand]
    if allow_manual and raw.lower() in MANUAL_OPS:
        return "manual"
    raise RuleError(
        f"不认识的原子操作 {op!r}；定义 8 的原子操作集是 "
        f"{sorted(OP_POLARITY)}（可写 aliases: thr_up / thr_down / stop_plus / stop_minus）"
    )


def set_rule(con: sqlite3.Connection, name: str, body: str, op: str,
             by: str = "", why: str = "") -> dict:
    """写一条规则的新版本，并把生效指针切过去。

    * 新版本节点 ``rule|<name>|v<n>``，旧版一个字都不动（A3）。
    * ``op`` 必须是原子操作（极性由它唯一决定）；``'manual'`` 表示非原子人工改写，
      只记账、不进极性账本（极性无法判定就不假装能判定）。
    * 写进去的正文必须先解析得动（否则审计第③项立刻红）。

    返回 ``{name, version, id, body, old, polarity, op, created}``。
    """
    name = _normalize_name(name)
    op = _canon_op(op, allow_manual=True)
    body = (body or "").strip()
    if not body:
        raise RuleError("新正文为空")
    parse_body(body)  # 先验：解析不了的不许进图

    anchor = _node(con, _anchor_uniq(name))
    if anchor is None:
        # 第一次写一条还没锚点的规则：锚点 body 就是这份正文（基线），不产生版本。
        db.node(con, "rule", body, uniq=_anchor_uniq(name))
        _log(con, name, "init", "", body, op, by, why, OP_POLARITY.get(op))
        con.commit()
        return {"name": name, "version": None, "id": None, "body": body,
                "old": "", "polarity": OP_POLARITY.get(op), "op": op, "created": True}

    cur_id = _current_id(con, name)
    old_id = cur_id if cur_id is not None else int(anchor["id"])
    old_body = _body_of(con, old_id)

    n = _next_version_number(con, name)
    ver = f"v{n}"
    vid = db.node(con, "rule", body, uniq=_version_uniq(name, n))
    polarity = OP_POLARITY.get(op)
    if polarity is not None:
        # 极性账本：版本链上一条带符号改写边
        db.edge(con, old_id, vid, REL_POS if polarity > 0 else REL_NEG)
    _point_current(con, name, vid)
    _log(con, name, ver, old_body, body, op, by, why, polarity)
    con.commit()
    return {"name": name, "version": ver, "id": vid, "body": body, "old": old_body,
            "polarity": polarity, "op": op, "created": False}


# ══════════════════════════════════════════════════════════════════
# 四、读：生效正文 / 版本链
# ══════════════════════════════════════════════════════════════════

def current(con: sqlite3.Connection, name: str) -> str:
    """当前生效正文：有 current 边读它指向的版本，否则读锚点 body。"""
    raw = (name or "").strip()
    if raw.startswith(ANCHOR_PREFIX):
        raw = raw[len(ANCHOR_PREFIX):]
    m = VERSION_RE.match(raw)
    if m:  # 直接给了版本名：就读那一版
        row = _node(con, _version_uniq(m.group("name"), int(m.group("n"))))
        return str(row["body"]) if row else ""
    n = _normalize_name(raw)
    cur_id = _current_id(con, n)
    if cur_id is not None:
        return _body_of(con, cur_id)
    anchor = _node(con, _anchor_uniq(n))
    return str(anchor["body"]) if anchor else ""


def current_version(con: sqlite3.Connection, name: str) -> str | None:
    """生效指针落在哪个版本：'v3' / None（还停在锚点）。"""
    name = _normalize_name(name)
    cur_id = _current_id(con, name)
    if cur_id is None:
        return None
    for n in _version_numbers(con, name):
        row = _node(con, _version_uniq(name, n))
        if row is not None and int(row["id"]) == cur_id:
            return f"v{n}"
    return f"id:{cur_id}"


def history(con: sqlite3.Connection, name: str) -> list[dict]:
    """版本链：``[基线锚点, v1, v2, ...]``，按版本号排。

    每项 ``{ver, id, body, current, op, polarity, by, why, when}``。
    基线锚点（ver='base'）永远在第一位——它是这条规则「有版本之前」的样子。
    """
    name = _normalize_name(name)
    cur_id = _current_id(con, name)
    out: list[dict] = []
    anchor = _node(con, _anchor_uniq(name))
    out.append({
        "ver": "base",
        "id": int(anchor["id"]) if anchor is not None else 0,
        "body": str(anchor["body"]) if anchor is not None else "",
        "current": anchor is not None and cur_id == int(anchor["id"]),
        "op": "", "polarity": None, "by": "", "why": "", "when": "",
    })
    for n in _version_numbers(con, name):
        row = _node(con, _version_uniq(name, n))
        if row is None:
            continue
        ver = f"v{n}"
        info = _log_info(con, name, ver)
        out.append({
            "ver": ver,
            "id": int(row["id"]),
            "body": str(row["body"]),
            "current": cur_id == int(row["id"]),
            "op": info.get("op", ""),
            "polarity": info.get("polarity"),
            "by": info.get("by", ""),
            "why": info.get("why", ""),
            "when": "",
        })
    return out


# ══════════════════════════════════════════════════════════════════
# 五、回退 = 把指针切回旧版本（不删任何东西）
# ══════════════════════════════════════════════════════════════════

def revert(con: sqlite3.Connection, name: str, to_version: str | int,
           by: str = "", why: str = "") -> dict:
    """回退到旧版本：只切指针，不新建版本、不删任何节点/边。

    同时补一条**反向带符号边**（当前版本 -> 目标版本），符号取「被撤销那次改写」
    的反相——定理 7：逆操作必反相。于是「改了什么又改回来」自动形成极性 -1 的 2-环，
    也就是定理 8 里 L1 的单圈。
    """
    name = _normalize_name(name)
    if isinstance(to_version, int):
        n = to_version
    else:
        m = re.match(r"^\s*v?(\d+)\s*$", str(to_version))
        if not m:
            raise RuleError(f"版本号看不懂：{to_version!r}（要 v1 / 1 这种）")
        n = int(m.group(1))
    target = _node(con, _version_uniq(name, n))
    if target is None:
        raise RuleError(f"没有这个版本：{name} v{n}")
    cur_id = _current_id(con, name)
    if cur_id is None:
        raise RuleError(f"{name} 还没有任何版本节点，回退无处可去（锚点就是唯一状态）")
    if cur_id == int(target["id"]):
        raise RuleError(f"{name} 已经就在 v{n}")

    # 逆操作必反相：找指向当前版本的那条前向改写边，反向边符号取反；找不到按 -1。
    fwd = con.execute(
        "SELECT rel FROM edges WHERE dst=? AND rel IN (?, ?) ORDER BY id DESC LIMIT 1",
        (cur_id, REL_POS, REL_NEG),
    ).fetchone()
    sign = -1 if (fwd is None or fwd["rel"] == REL_POS) else +1
    db.edge(con, cur_id, int(target["id"]), REL_POS if sign > 0 else REL_NEG)
    cur_ver = current_version(con, name) or "?"
    _point_current(con, name, int(target["id"]))

    n_rev = len(list(con.execute(
        "SELECT 1 FROM nodes WHERE kind='rulelog' AND uniq LIKE ?",
        (f"{LOG_PREFIX}{name}|r%",),
    )))
    _log(con, name, f"r{n_rev + 1}", _body_of(con, cur_id), _body_of(con, int(target["id"])),
         "revert", by, why, sign)
    con.commit()
    return {"name": name, "from": cur_ver, "to": f"v{n}", "polarity": sign,
            "body": _body_of(con, int(target["id"]))}


# ══════════════════════════════════════════════════════════════════
# 六、六种原子操作：各自产出新正文 + 极性谓词
# ══════════════════════════════════════════════════════════════════

def _op_target(name: str, op: str) -> str:
    """stop± 的默认对象是停用表（rule|不入记忆）；其它操作必须点名。"""
    if name:
        return name
    if op in ("stop+", "stop-"):
        return "不入记忆"
    raise RuleError(f"{op} 必须指定规则名")


def _need(payload: dict, key: str, op: str):
    if key not in payload or payload[key] in (None, ""):
        raise RuleError(f"{op} 缺少参数 {key!r}")
    return payload[key]


def _apply_to_body(parsed: dict, op: str, payload: dict) -> str:
    """在解析结果上执行原子操作，返回新正文；语义不对就抛错（不猜）。"""
    title = parsed["title"]
    if op in ("thr↑", "thr↓"):
        if parsed["form"] != "kv":
            raise RuleError(f"{op} 只对键值型规则体有效，这条是词表型")
        key = str(_need(payload, "key", op))
        to = _as_number(_need(payload, "to", op))
        kvs = list(parsed["kvs"])
        idx = next((i for i, (k, _) in enumerate(kvs) if k == key), None)
        if idx is None:
            raise RuleError(f"规则体里没有键 {key!r}（现有 {[k for k, _ in kvs]}）")
        old = _as_number(kvs[idx][1])
        if op == "thr↑" and not (to > old):
            raise RuleError(f"thr↑ 要求放宽（数值变大），当前 {old} → 目标 {to} 不是放宽")
        if op == "thr↓" and not (to < old):
            raise RuleError(f"thr↓ 要求收紧（数值变小），当前 {old} → 目标 {to} 不是收紧")
        lo, hi = RULE_BOUNDS.get(key, (float("-inf"), float("inf")))
        if not (lo <= float(to) <= hi):
            raise RuleError(f"{key}={to} 越界（合法区间 [{lo}, {hi}]）")
        num = int(to) if float(to).is_integer() else to
        kvs[idx] = (key, str(num))
        return _fmt_kv(title, kvs)

    # stop± 本来是给词表型规则体用的（操作词、不入记忆那种一整列词）。
    # 但触发规则（`rule|触发·*`）的触发词藏在 kv 型的 `when` 键里，
    # 要能做到"改触发规则自己"，stop± 就必须能作用到**某个键的值**上。
    # 调用方式：payload = {"key": "when", "w": "多记"}。
    if op in ("stop+", "stop-") and parsed["form"] == "kv" and payload.get("key"):
        target_key = str(payload["key"])
        word = str(_need(payload, "w", op))
        kvs = list(parsed["kvs"])
        idx = next((i for i, (k, _) in enumerate(kvs) if k == target_key), None)
        if idx is None:
            raise RuleError(f"没有这个键：{target_key}")
        words = [x.strip() for x in kvs[idx][1].split("、") if x.strip()]
        if op == "stop+":
            if word in words:
                words.remove(word)
        else:
            if word not in words:
                words.append(word)
        if not words:
            raise RuleError(f"{target_key} 不能清空——删到最后一个词就等于废掉这条规则，"
                            f"该用 supersedes 而不是 stop+")
        kvs[idx] = (target_key, "、".join(words))
        return _fmt_kv(title, kvs)

    if parsed["form"] != "list":
        raise RuleError(f"{op} 只对词表型规则体有效，这条是键值型")
    items = list(parsed["items"])
    sep = parsed["sep"]

    if op == "merge":
        a = str(_need(payload, "a", op))
        b = str(_need(payload, "b", op))
        if a not in items:
            raise RuleError(f"合并源 {a!r} 不在词表里（适用集里没有它，谈不上并入）")
        if a == b:
            raise RuleError("merge 的 a 与 b 相同，等于没改")
        items = [b if it == a else it for it in items]
        if b not in items:      # b 的适用集扩大：a 的实例并进来后 b 才出现
            items.append(b)
        return _fmt_list(title, items, sep)

    if op == "stop+":
        w = str(_need(payload, "w", op))
        if w in items:
            raise RuleError(f"{w!r} 已在词表里，stop+ 要求当时不在")
        items.append(w)
        return _fmt_list(title, items, sep)

    if op == "stop-":
        w = str(_need(payload, "w", op))
        if w not in items:
            raise RuleError(f"{w!r} 不在词表里，stop- 没东西可移出")
        items.remove(w)
        return _fmt_list(title, items, sep)

    if op == "split":
        a = str(_need(payload, "a", op))
        into = _need(payload, "into", op)
        if isinstance(into, str):
            into = [x.strip() for x in re.split(r"[、,，／/]", into) if x.strip()]
        if not isinstance(into, (list, tuple)) or len(into) < 2:
            raise RuleError("split 的 into 要给至少两个词（一义拆二）")
        if a not in items:
            raise RuleError(f"拆分源 {a!r} 不在词表里")
        into = [str(x) for x in into]
        i = items.index(a)
        items[i:i + 1] = into
        return _fmt_list(title, items, sep)

    raise RuleError(f"未实现的原子操作：{op}")


def apply_op(con: sqlite3.Connection, name: str, op: str, payload: dict | None = None,
             by: str = "", why: str = "") -> dict:
    """执行一次原子操作：新正文 + 极性谓词（谓词由 op 决定，不由调用者传）。

    返回 ``{name, op, polarity, version, old, new, id, ...}``。
    """
    op = _canon_op(op)
    payload = dict(payload or {})
    name = _op_target(str(name or ""), op)
    old = current(con, name)
    if not old:
        raise RuleError(f"规则不存在或没有正文：{name}")
    parsed = parse_body(old)
    new = _apply_to_body(parsed, op, payload)
    res = set_rule(con, name, new, op, by, why)
    res.update({"op": op, "old": old, "new": new})
    return res


# ══════════════════════════════════════════════════════════════════
# 七、带符号环检测（定理 8：L1 = 单圈极性 -1，L2 = 双圈 +1）
# ══════════════════════════════════════════════════════════════════

def cycles(con: sqlite3.Connection, max_len: int = 4) -> list[dict]:
    """枚举带符号改写图上长度 2..max_len 的简单环，返回环列表。

    每项 ``{规则: [名字...], 极性: ±1, 长度, 节点, 边, 自指}``；
    极性 = 路径上谓词符号之积（Z/2）。只认两端都是 kind='rule' 的
    ``rewrites`` / ``unrewrites`` 边——也就是定义 9 那条 groupoid 的骨架。
    """
    max_len = max(2, min(int(max_len), 8))
    rows = con.execute(
        "SELECT e.id AS eid, e.src AS src, e.dst AS dst, e.rel AS rel, "
        "       sn.uniq AS suniq, dn.uniq AS duniq "
        "  FROM edges e "
        "  JOIN nodes sn ON sn.id = e.src "
        "  JOIN nodes dn ON dn.id = e.dst "
        " WHERE e.rel IN (?, ?) AND sn.kind = 'rule' AND dn.kind = 'rule' "
        " ORDER BY e.id",
        (REL_POS, REL_NEG),
    ).fetchall()
    if not rows:
        return []

    out: dict[int, list[dict]] = {}
    uniq_of: dict[int, str] = {}
    for r in rows:
        src, dst = int(r["src"]), int(r["dst"])
        uniq_of[src], uniq_of[dst] = str(r["suniq"]), str(r["duniq"])
        out.setdefault(src, []).append({
            "dst": dst, "sign": 1 if r["rel"] == REL_POS else -1,
            "rel": str(r["rel"]), "eid": int(r["eid"]),
        })

    found: list[dict] = []
    starts = sorted(uniq_of)
    for start in starts:
        nodes_path = [start]
        edges_path: list[dict] = []
        visited = {start}

        def walk(v: int, acc: int) -> None:
            for e in out.get(v, ()):
                nxt = e["dst"]
                length = len(edges_path) + 1
                if nxt == start:
                    if length >= 2:            # 长度 1 的自环不算环（定义 9 要求 |P|>0 的往返）
                        edges_path.append(e)
                        node_uniqs = [uniq_of[x] for x in nodes_path]
                        edge_uniqs = node_uniqs + [uniq_of[start]]   # 闭合那一步
                        names = [_rule_name(u) for u in node_uniqs]
                        found.append({
                            "规则": names,
                            "极性": acc * e["sign"],
                            "长度": len(edges_path),
                            "节点": node_uniqs,
                            "边": [
                                {"src": edge_uniqs[i], "dst": edge_uniqs[i + 1],
                                 "rel": edges_path[i]["rel"], "极性": edges_path[i]["sign"]}
                                for i in range(len(edges_path))
                            ],
                            "自指": len(set(names)) == 1,
                        })
                        edges_path.pop()
                elif nxt not in visited and length < max_len and nxt > start:
                    # 只走 id 更大的点 => 每个环只从最小 id 出发被枚举一次（去重）
                    visited.add(nxt)
                    nodes_path.append(nxt)
                    edges_path.append(e)
                    walk(nxt, acc * e["sign"])
                    edges_path.pop()
                    nodes_path.pop()
                    visited.discard(nxt)

        walk(start, 1)

    found.sort(key=lambda c: (c["长度"], c["极性"], c["规则"], c["节点"]))
    return found


# ══════════════════════════════════════════════════════════════════
# 八、外部一致性审计（不经过写入路径）
# ══════════════════════════════════════════════════════════════════

_AUDIT_CODES = {
    "multi-current": "①同名多 current 指针",
    "current-dangling": "②current 指向不存在的节点",
    "body": "③body 解析失败",
    "bounds": "④阈值越界",
}


def audit(con: sqlite3.Connection) -> dict:
    """独立读出全部规则做四项体检，返回 ``{ok, rules, versions, current_edges, problems}``。

    这里**故意不用 current() / set_rule()**：自己的 SQL、自己的解析。
    对应 v0.2 边界 3——自指不能保证一致性，得有一把站在外面的尺子。
    """
    problems: list[dict] = []

    nodes = list(con.execute("SELECT id, kind, body, uniq FROM nodes ORDER BY id"))
    node_by_id = {int(r["id"]): r for r in nodes}
    # 被 `supersedes` 取代的旧节点不参与统计——它们按纪律不能删，但也不该算数。
    # 早先没排除，规则数被算成 29（真正生效 21），审计看着"通过"但账是错的。
    superseded = {int(r["dst"]) for r in con.execute(
        "SELECT dst FROM edges WHERE rel='supersedes'")}
    rule_rows = [r for r in nodes
                 if r["kind"] == "rule" and int(r["id"]) not in superseded]

    anchors: dict[str, sqlite3.Row] = {}
    versions: dict[str, list[tuple[int, sqlite3.Row]]] = {}
    for r in rule_rows:
        uniq = str(r["uniq"])
        if uniq.startswith("trigger|"):
            # 触发规则也是规则（kind='rule'），但它的 uniq 用 `trigger|` 前缀，
            # 以便和配置型规则区分。审计只做「正文能不能解析」，不套规则的命名约束。
            try:
                parse_body(str(r["body"]))
            except Exception as e:
                problems.append({"code": "body", "rule": uniq, "detail": str(e)[:80]})
            continue
        if not uniq.startswith(ANCHOR_PREFIX):
            problems.append({"code": "body", "rule": _rule_name(uniq),
                             "detail": f"kind='rule' 但 uniq 不以 {ANCHOR_PREFIX!r} 开头：{uniq!r}"})
            continue
        body_uniq = uniq[len(ANCHOR_PREFIX):]
        m = VERSION_RE.match(body_uniq)
        if m:
            versions.setdefault(m.group("name"), []).append((int(m.group("n")), r))
        else:
            anchors[body_uniq] = r

    # ③ body 解析失败：所有规则节点（含历史版本）都得解析得动
    for r in rule_rows:
        try:
            parse_body(str(r["body"]))
        except RuleError as exc:
            problems.append({"code": "body", "rule": _rule_name(str(r["uniq"])),
                             "detail": f"节点 {int(r['id'])}（{r['uniq']}）：{exc}"})

    # ① 同名多 current 指针 & ② current 指向不存在的节点
    cur_edges = list(con.execute(
        "SELECT e.id AS eid, e.src AS src, e.dst AS dst FROM edges e WHERE e.rel='current' ORDER BY e.id"))
    per_name: dict[str, list[int]] = {}
    for e in cur_edges:
        src_row = node_by_id.get(int(e["src"]))
        if src_row is None:
            problems.append({"code": "current-dangling", "rule": "?",
                             "detail": f"current 边 id={int(e['eid'])} 的源锚点 {int(e['src'])} 不存在"})
            continue
        name = _rule_name(str(src_row["uniq"]))
        per_name.setdefault(name, []).append(int(e["eid"]))
        dst_row = node_by_id.get(int(e["dst"]))
        if dst_row is None:
            problems.append({"code": "current-dangling", "rule": name,
                             "detail": f"current 边 id={int(e['eid'])} 指向不存在的节点 {int(e['dst'])}"})
            continue
        if dst_row["kind"] != "rule" or _rule_name(str(dst_row["uniq"])) != name:
            problems.append({"code": "current-dangling", "rule": name,
                             "detail": f"current 边 id={int(e['eid'])} 指向 {dst_row['uniq']!r}，不是本规则的版本"})
    for name, eids in per_name.items():
        if len(eids) > 1:
            problems.append({"code": "multi-current", "rule": name,
                             "detail": f"同名 {name} 有 {len(eids)} 条 current 边（id={eids}），生效指针不唯一"})

    # ④ 阈值越界：只查生效正文（历史版本不生效，不按越界算）
    for name, anchor in anchors.items():
        eids = per_name.get(name, [])
        effective = str(anchor["body"])
        if eids:
            row = con.execute("SELECT dst FROM edges WHERE id=?", (eids[-1],)).fetchone()
            if row is not None and int(row["dst"]) in node_by_id:
                effective = str(node_by_id[int(row["dst"])]["body"])
        try:
            parsed = parse_body(effective)
        except RuleError:
            continue  # ③ 已经报过
        if parsed["form"] != "kv":
            continue
        for k, raw in parsed["kvs"]:
            if k not in RULE_BOUNDS:
                continue
            lo, hi = RULE_BOUNDS[k]
            try:
                val = float(raw)
            except ValueError:
                problems.append({"code": "bounds", "rule": name,
                                 "detail": f"{k}={raw!r} 不是数字（合法区间 [{lo}, {hi}]）"})
                continue
            if not (lo <= val <= hi):
                problems.append({"code": "bounds", "rule": name,
                                 "detail": f"{k}={raw} 越界（合法区间 [{lo}, {hi}]）"})

    problems.sort(key=lambda p: (p["code"], p["rule"], p["detail"]))
    return {
        "ok": not problems,
        "rules": len(anchors),
        "versions": sum(len(v) for v in versions.values()),
        "current_edges": len(cur_edges),
        "problems": problems,
        "code_names": dict(_AUDIT_CODES),
    }
