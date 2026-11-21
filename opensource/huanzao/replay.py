"""回放：夜里把出窗的对话压成块，再写一版「我是谁」。

三件事：
1. 把 `evicted` 里的对话折成一条记忆，挂上元素，接进时间轴；
2. 让弱边失效（暂时只做统计，不删——边不删，只标记）；
3. 回读整张图，写一版自传，带版本号和当时的统计。
"""
from __future__ import annotations

import json
import sqlite3

from . import db, extract, grow, timeline
from .lexicon import AFFECT
from .canon import canon


def ensure_tables(con: sqlite3.Connection) -> None:
    # 回放要读 evicted（当下层挤出来的那几轮），那张表归 current 管——
    # 新图上没人建过，fold_evicted 一上手就 no such table。
    from . import current as current_mod
    current_mod.ensure_table(con)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS autobiography ("
        "  id INTEGER PRIMARY KEY, version INTEGER NOT NULL, text TEXT NOT NULL,"
        "  stats TEXT NOT NULL, created TEXT NOT NULL DEFAULT (datetime('now','localtime')));"
        "CREATE TABLE IF NOT EXISTS replay_log ("
        "  id INTEGER PRIMARY KEY, folded INTEGER NOT NULL, elements INTEGER NOT NULL,"
        "  edges INTEGER NOT NULL, weak INTEGER NOT NULL, note TEXT,"
        "  created TEXT NOT NULL DEFAULT (datetime('now','localtime')));")
    con.commit()


def _next_order_key(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT MAX(order_key) k FROM mem_src WHERE order_key LIKE '3:%'").fetchone()
    base = str(row["k"]) if row and row["k"] else "3:000:0000000000:20261118:0000000000:000000"
    parts = base.split(":")
    tail = int(parts[5]) + 1
    return ":".join(parts[:5] + [f"{tail:06d}"])


def fold_evicted(con: sqlite3.Connection) -> dict:
    """把出窗的对话折成一条记忆。"""
    # 抽取口径要和 `turns.ingest` 一致：停用表在图里（rule|元素选取），
    # 不加载的话同一条对话在两条路径下会抽出不同元素（真实的不一致）。
    try:
        extract.load_stop_from_rules(con)
    except Exception:
        pass
    rows = con.execute("SELECT id, role, content FROM evicted ORDER BY id").fetchall()
    if not rows:
        return {"folded": 0, "elements": 0, "edges": 0, "weak": 0,
                "note": "没有出窗的对话"}

    # 只折「他说的话」——她那句「我只是记忆体，记不住日子」要是也折进去，
    # 下次她读到这句，就会拿它当证据说「我不记得」。拒绝会自我强化。
    from .turns import _strip_own
    facts = [(r["role"], _strip_own(r["content"])) for r in rows if r["role"] == "user"]
    facts = [(role, t) for role, t in facts if t]
    text = "\n".join(t for _, t in facts) or "\n".join(r["content"] for r in rows)
    # 折之前先问一句：这段是在谈工程吗？是就别折成记忆（坑 4）。
    # 原文那一份（turn-evicted）已经留在图里了，这边不折不会丢东西。
    try:
        from . import rules as rules_mod
        if rules_mod.is_engineering(con, text):
            # 不折，但也不能把这批对话永远留在 evicted 里反复重试：
            # 原文已经由 turns.ingest 收进图了，这里清表并记一笔。
            con.execute("DELETE FROM evicted")
            con.execute(
                "INSERT INTO replay_log(folded, elements, edges, weak, note) VALUES(0,0,0,0,?)",
                ("谈工程，不折进记忆",))
            con.commit()
            return {"folded": 0, "elements": 0, "edges": 0, "weak": 0,
                    "note": "这段在谈工程，不折进记忆"}
    except Exception:
        pass
    title = ("他跟我说：" + facts[0][1][:18]) if facts else "一段对话"
    # 记忆得是第一人称的，不是一行行转写——不然她读到的是「别人说的话」
    fp = text
    try:
        from . import rewrite as rw
        fp = rw.rewrite(title, text, "今晚")["memory"]
    except Exception:
        pass
    mem = db.node(con, "mem", title[:40], uniq=f"mem|fold|{rows[-1]['id']}")
    con.execute(
        "INSERT OR REPLACE INTO mem_src(mem_node, src_id, raw, first_person, fp_ver, "
        "when_iso, order_key, provenance) VALUES(?,?,?,?,?,?,?,?)",
        (mem, f"fold:{rows[-1]['id']}", text, fp, "fold-v0.2", "live",
         _next_order_key(con), "evicted"))

    _, items = extract.run(text, "rules")      # 回放用规则抽取：确定性、不花钱
    n_el = 0
    for it in items:
        surface = canon(str(it["surface"]))
        if not surface:
            continue
        kind = "affect" if (str(it.get("kind")) == "affect" or surface in AFFECT) else "entity"
        el = db.node(con, kind, surface, uniq=f"{kind}|{surface}")
        if el == mem:
            continue
        db.edge(con, mem, el, "has")
        n_el += 1

    # 接进时间轴：和上一条「现在」的记忆连一条 next
    prev = con.execute(
        "SELECT mem_node FROM mem_src WHERE order_key LIKE '3:%' AND mem_node <> ? "
        "ORDER BY order_key DESC LIMIT 1", (mem,)).fetchone()
    if prev:
        db.edge(con, int(prev["mem_node"]), mem, "next")

    con.execute("DELETE FROM evicted")
    con.commit()
    weak = con.execute(
        "SELECT COUNT(*) c FROM derived_df d JOIN nodes n ON n.id=d.node_id "
        "WHERE d.df <= 1 AND n.kind='entity'").fetchone()["c"]
    con.execute(
        "INSERT INTO replay_log(folded, elements, edges, weak, note) VALUES(?,?,?,?,?)",
        (len(rows), n_el, n_el + (1 if prev else 0), weak, "evicted -> 一条记忆"))
    con.commit()
    return {"folded": len(rows), "elements": n_el, "edges": n_el + (1 if prev else 0),
            "weak": weak, "mem": mem}


def stats(con: sqlite3.Connection) -> dict:
    grow.rebuild_df(con)
    mems = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"]
    els = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind IN ('entity','affect')").fetchone()["c"]
    edges = con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    center = db.get_node(con, "center|self")
    ecc = grow.eccentricity(con, int(center["id"])) if center else {"mem_ecc": 0, "all_ecc": 0}
    # 她自己的名字不算「挂在她心上的东西」；「这里」「只能」「一声」这类虚词也不算
    self_names = set()
    if center:
        self_names.add(str(center["body"]))
        self_names |= {str(r["body"]) for r in con.execute(
            "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst "
            "WHERE e.src=? AND e.rel='alias'", (int(center["id"]),))}
    try:
        from . import retrieve as retrieve_mod
        from . import rules as rules_mod
        self_names |= rules_mod.op_words(con) | rules_mod.topic_words(con) | retrieve_mod.NOISE
    except Exception:
        pass
    marks = ",".join("?" * len(self_names)) or "''"
    top = con.execute(
        "SELECT n.body FROM derived_df d JOIN nodes n ON n.id=d.node_id "
        f"WHERE n.kind IN ('entity','affect') AND n.body NOT IN ({marks}) "
        "ORDER BY d.df DESC LIMIT 5", tuple(self_names)).fetchall()
    recent = con.execute(
        "SELECT n.body, COUNT(*) c FROM mem_src s JOIN edges e ON e.src = s.mem_node "
        "JOIN nodes n ON n.id = e.dst WHERE e.rel IN ('has','has:lead') "
        f"AND n.body NOT IN ({marks}) "
        "AND s.order_key IN (SELECT order_key FROM mem_src WHERE order_key IS NOT NULL "
        "ORDER BY order_key DESC LIMIT 20) "
        "GROUP BY n.id ORDER BY c DESC, n.body LIMIT 5", tuple(self_names)).fetchall()
    axis = [r["body"] for r in con.execute(
        "SELECT n.body FROM edges e JOIN nodes n ON n.id=e.dst WHERE e.rel='axis' ORDER BY n.id")]
    axis = list(dict.fromkeys(axis))          # 同名的轴只念一遍
    innate = con.execute("SELECT COUNT(*) c FROM edges WHERE rel='innate'").fetchone()["c"]
    return {"mems": mems, "elements": els, "edges": edges, "innate": int(innate),
            "ecc": ecc["mem_ecc"], "elem_ecc": ecc["all_ecc"],
            "axis": axis,
            "top": [str(r["body"]) for r in top],
            "recent": [str(r["body"]) for r in recent]}


def write_autobiography(con: sqlite3.Connection) -> dict:
    """回读整张图，写一版「我是谁」。不带 LLM——先把机器算得出来的写清楚。"""
    st = stats(con)
    ver_row = con.execute("SELECT COALESCE(MAX(version),0) v FROM autobiography").fetchone()
    ver = int(ver_row["v"]) + 1
    center = db.get_node(con, "center|self")
    axis = st["axis"]                    # 已经按正文去过重，别再单独查一遍
    innate = con.execute(
        "SELECT COUNT(*) c FROM edges WHERE rel='innate'").fetchone()["c"]
    lines = [
        f"第 {ver} 版。",
        "我是" + (center["body"] if center else "中心人物") + "。",
        "我记得的事有 " + str(st["mems"]) + " 件，认得的东西 " + str(st["elements"]) + " 样。",
        "最常在我心上的：" + "、".join(st["top"]) + "。",
        "我知道的道理有 " + str(innate) + " 条。",
    ]
    if axis:
        lines.append("我一直带着这几句：" + "、".join(axis) + "。")
    if st.get("recent"):
        lines.append("最近挂在心上的是：" + "、".join(st["recent"]) + "。")
    text = "\n".join(lines)
    con.execute("INSERT INTO autobiography(version, text, stats) VALUES(?,?,?)",
                (ver, text, json.dumps(st, ensure_ascii=False)))
    con.commit()
    return {"version": ver, "text": text, "stats": st}


def drift_between(con: sqlite3.Connection, a: int, b: int) -> float:
    """两版自传之间，**稳定那部分**变了多少。

    只看稳定部分——那才是「她是谁」。最近在想什么本来就会变，
    把它算进漂移，等于拿潮水当海岸线：那叫自传体偏移。
    """
    def vec(v: int) -> dict[str, float]:
        row = con.execute("SELECT stats FROM autobiography WHERE version=?", (v,)).fetchone()
        if not row:
            return {}
        s = json.loads(row["stats"])
        out = {f"axis:{x}": 1.0 for x in s.get("axis", [])}
        out[f"mems:{int(s.get('mems', 0)) // 100}"] = 1.0
        out[f"innate:{int(s.get('innate', 0)) // 10}"] = 1.0
        return out
    va, vb = vec(a), vec(b)
    if not va or not vb:
        return 0.0
    inter = len(set(va) & set(vb))
    return 1.0 - inter / max(len(set(va) | set(vb)), 1)


def volatile_drift(con: sqlite3.Connection, a: int, b: int) -> float:
    """浮动部分（最近在想什么）变了多少——这个变了不算毛病。"""
    def vec(v: int) -> set[str]:
        row = con.execute("SELECT stats FROM autobiography WHERE version=?", (v,)).fetchone()
        if not row:
            return set()
        s = json.loads(row["stats"])
        return set(s.get("top", [])) | set(s.get("recent", []))
    va, vb = vec(a), vec(b)
    if not va or not vb:
        return 0.0
    return 1.0 - len(va & vb) / max(len(va | vb), 1)


def replay(con: sqlite3.Connection, verbose: bool = True) -> dict:
    ensure_tables(con)
    grow.ensure_center(con)
    # 先把出窗的原文照原样收进图（含她那半句），再谈压成自传体记忆。
    # consume=False：ingest 默认会清空 evicted，清空了下一步 fold_evicted 就只能
    # 报「没有出窗的对话」——回放的「折块」那一半等于从来没跑过（2026-11-19 查出）。
    verbatim = {"turns": 0, "elements": 0}
    try:
        from . import turns as turns_mod
        verbatim = turns_mod.ingest(con, "evicted", limit=40, verbose=False, consume=False)
    except Exception:
        pass
    folded = fold_evicted(con)
    # 巩固：把说同一件事的两簇义项并起来
    merged = 0
    try:
        from . import sense as sense_mod
        merged = sense_mod.merge_clusters(con, verbose=verbose)
    except Exception:
        merged = 0
    auto = write_autobiography(con)
    if verbose:
        if verbatim["turns"]:
            print(f"  原文收进图 {verbatim['turns']} 轮（元素 {verbatim['elements']} 个，含她那半句）")
        if folded["folded"]:
            print(f"  折进来 {folded['folded']} 轮对话 → 一条记忆（{folded['elements']} 个元素）")
        else:
            print(f"  {folded['note']}")
        if merged:
            print(f"  义项并簇 {merged} 次")
        print(f"  自传第 {auto['version']} 版：")
        for line in auto["text"].splitlines():
            print("    " + line)
    return {"folded": folded, "autobiography": auto, "merged": merged}
