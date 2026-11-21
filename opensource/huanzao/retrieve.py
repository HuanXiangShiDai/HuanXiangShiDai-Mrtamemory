"""三路召回的最小版：字面 / 结构 / 状态。

字面：用户这句话里命中了哪些元素（含别名义项）
结构：命中的元素在图上能连到哪些记忆（一跳）
状态：眼下情绪与元素签名的共振（暂留接口）

输出：元素集合 + 它们指向的记忆（按加权覆盖率排）。
"""
from __future__ import annotations

import re
import sqlite3

from .lexicon import AFFECT

STOP_SHORT = set("的了是在有和与就不都很太")
# 寒暄与虚词构成的「元素」是噪声：它们谁都能配上，配上也没用
NOISE = set("在吗 好吗 对吧 是不是 知道吗 什么 怎么 这样 那样 我们 你们 他们 你好 拜拜 "
            "这些 那些 这个 那个 现在 一下 一个 内容 东西 事情 问题 看看 截图 "
            "这里 那里 这边 那边 一声 只能 没别".split())

# 话题词：谈的是「系统／机制」，不是「她的往事」。命中它们不当种子——
# 「记忆」这个词对得上、意思不对，翻出来全是无关的旧片段。
META = set("记忆 记忆库 元图 元素 义项 规则 上下文 系统 工具 插件 模型 向量 权重 节点 边 "
           "框架 文档 代码 配置 接口 调用 注入 缓存 提示词".split())
_FUNC_CHARS = set("在吗呢吧啊呀哦嗯了的是在有和与就不都也很太一这那你我他她它们个")


def _is_noise(body: str) -> bool:
    if body in NOISE or body in META:
        return True
    if len(body) <= 4 and all(ch in _FUNC_CHARS for ch in body):
        return True
    return False


def _norm(s: str) -> str:
    return (s or "").strip()


def hit_elements(con: sqlite3.Connection, text: str, limit: int = 12,
                 act: dict[int, float] | None = None) -> list[sqlite3.Row]:
    """字面命中：元素名出现在这句话里。近期被提到过的排前面。

    情绪词允许一个字（累、笑、哭），别的至少要两个字。
    """
    rows = con.execute("SELECT id, body, kind FROM nodes WHERE kind IN ('entity','affect')").fetchall()
    hits = []
    for r in rows:
        body = r["body"]
        if not body or _is_noise(body):
            continue
        if len(body) == 1:
            if r["kind"] == "affect" and body in text:
                hits.append(r)
            continue
        if body in text:
            hits.append(r)
    act = act or {}
    # 泛词不当种子：出现超过三成记忆的元素，认它也没用
    total = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"] or 1
    df = {int(r["node_id"]): int(r["df"]) for r in con.execute(
        "SELECT node_id, df FROM derived_df")}
    # 操作词从图里的规则节点读（规则可改，改数据不改代码）
    try:
        from .rules import op_words, topic_words
        ops = op_words(con) | topic_words(con)
    except Exception:
        ops = set()
    kept = []
    weak = 0
    for r in hits:
        nid = int(r["id"])
        share = df.get(nid, 0) / total
        if str(r["body"]) in ops:
            continue          # 操作词：指向要做的事，不指向往事
        if df.get(nid, 0) == 0:
            continue          # 孤儿元素：没有任何记忆引用它，命中它等于命中空气
        # 小图不启用 30% 硬线：total 只有几条时任何元素都占 100%，
        # 一刀切会让「init + 喂一句话 + 问」永远空（实测踩到）。
        if total >= 4 and r["kind"] != "affect" and share > 0.3:
            continue
        # 「问题」「这些」这种到处都是的词：单独出现时不当种子
        if r["kind"] != "affect" and share > 0.05:
            weak += 1
        kept.append((r, share))
    strong = [r for r, share in kept if r["kind"] == "affect" or share <= 0.05]
    # 泛词线是相对的，不是绝对 5%。这张图上人物名天生就占得宽（乙 54%、甲 12%、
    # 渡鸦 6%），按 5% 一刀切，问谁都是空——人物恰好是最该被想起来的那个维度。
    # 退一步：没有锐词时，只交出「最不泛」的一两个，仍不许整句话都靠背景词撑起来。
    if not strong and kept:
        strong = [r for r, _ in sorted(kept, key=lambda kv: kv[1])[:2]]
    # 只有泛词就不当种子：宁可这一轮想不起来，也不要翻一段不相干的往事
    if not strong:
        return []
    hits = strong
    hits.sort(key=lambda r: (df.get(int(r["id"]), 0), -act.get(int(r["id"]), 0.0),
                            -len(r["body"]), r["body"]))
    return hits[:limit]


def memories_of(con: sqlite3.Connection, elem_ids: list[int], limit: int = 6) -> list[dict]:
    """结构：拿元素去找记忆，用加权覆盖率——命中稀有元素更值钱。"""
    if not elem_ids:
        return []
    total = con.execute("SELECT COUNT(*) c FROM nodes WHERE kind='mem'").fetchone()["c"] or 1
    marks = ",".join(str(int(i)) for i in elem_ids)
    rows = con.execute(
        f"SELECT e.src AS mem, e.dst AS el, COUNT(*) c FROM edges e "
        f"WHERE e.rel IN ('has','has:lead') AND e.dst IN ({marks}) GROUP BY e.src, e.dst"
    ).fetchall()
    df = dict(con.execute(
        f"SELECT dst, COUNT(DISTINCT src) FROM edges WHERE rel IN ('has','has:lead') "
        f"AND dst IN ({marks}) GROUP BY dst").fetchall())

    import math
    idf = {el: math.log(total / max(1, df.get(el, 1))) for el in elem_ids}
    need = sum(idf.values()) or 1.0
    score: dict[int, float] = {}
    foc: dict[int, tuple[float, int]] = {}
    for r in rows:
        mem = int(r["mem"])
        w = idf[int(r["el"])] * min(int(r["c"]), 3)
        score[mem] = score.get(mem, 0.0) + w
        if w > foc.get(mem, (0.0, 0))[0]:
            foc[mem] = (w, int(r["el"]))
    out = []
    for mem, s in sorted(score.items(), key=lambda kv: -kv[1])[:limit]:
        out.append({"mem": mem, "cover": min(1.0, s / need), "raw": s,
                    "focus_id": foc.get(mem, (0.0, 0))[1]})
    return out


def focus_text(con: sqlite3.Connection, elem_id: int) -> str:
    row = con.execute("SELECT body FROM nodes WHERE id=?", (elem_id,)).fetchone()
    return str(row["body"]) if row else ""


TIME_WORDS = ("下周", "上周", "昨天", "明天", "今天", "上次", "刚才", "之前", "最近",
              "后来", "之后", "那件事", "那个事", "说过的", "记不记得")


def recent_memories(con: sqlite3.Connection, n: int = 2) -> list[dict]:
    """最近折进来的几条。问「上次/下周」这类话时，它们该被翻出来。"""
    rows = con.execute(
        "SELECT mem_node FROM mem_src WHERE order_key IS NOT NULL "
        "ORDER BY order_key DESC LIMIT ?", (n,)).fetchall()
    return [{"mem": int(r["mem_node"]), "cover": 0.5, "raw": 0.0} for r in rows]


TALK_WORDS = ("上次聊", "刚才说", "之前说", "你说过", "我说过", "聊过", "说过什么",
              "记得我们", "上次说", "刚才聊", "我们聊", "聊",
              "说到哪", "讲到哪", "聊到哪")


def asks_about_talk(text: str) -> bool:
    """他是不是在问「我们之前说过什么」——只有这种才该翻对话碎片。

    「之前遗留的问题」不算：那是说工作，不是翻聊天记录。
    """
    return any(w in text for w in TALK_WORDS)


def mentions_time(text: str) -> bool:
    return any(w in text for w in TIME_WORDS)


def render_mem(con: sqlite3.Connection, mem: int, focus: str = "") -> dict:
    row = con.execute(
        "SELECT n.body AS title, s.first_person, s.raw, s.when_iso, s.order_key "
        "FROM nodes n LEFT JOIN mem_src s ON s.mem_node = n.id WHERE n.id = ?", (mem,)).fetchone()
    text = _norm((row["first_person"] or row["raw"] or "")) if row else ""
    if focus and focus in text:
        i = text.index(focus)
        text = ("…" if i > 25 else "") + text[max(0, i - 30): i + 60] + "…"
    return {
        "title": _norm(row["title"]) if row else "",
        "text": text,
        "when": (row["when_iso"] or "") if row else "",
    }


def describe_when(con: sqlite3.Connection, mem: int) -> str:
    """这条记忆落在人生的哪一段——给壳子看的人话时间。

    `when_iso` 对往事是 'canon'，直接念给她听等于「时间不明」。
    往事有叙事坐标，现在有日历：前者说清是哪一章，后者说清是哪天几点。
    """
    row = con.execute(
        "SELECT order_key, src_id, when_iso FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    if not row:
        return ""
    parts = str(row["order_key"] or "").split(":")
    seg = parts[0] if parts else ""
    if seg == "3":
        day = parts[3] if len(parts) > 3 else ""
        if len(day) == 8 and day not in ("00000000", "99999999"):
            t = parts[4] if len(parts) > 4 else "000000"
            hm = f" {t[:2]}:{t[2:4]}" if t and t != "000000" else ""
            return f"{day[:4]}-{day[4:6]}-{day[6:]}{hm}"
        return "现在（原文没留日子）"
    if seg == "2":
        src = str(row["src_id"] or "").split("#")[0]
        # 「主线剧情：某处旧址-05（时间线-主线28之后）」→「某处旧址-05」
        src = re.sub(r"^主线剧情：", "", src)
        src = re.sub(r"（[^）]*）$", "", src)
        return src or f"乐土·ch{parts[1] if len(parts) > 1 else '?'}"
    if seg == "1":
        return "生前"
    return "先天设定"


def describe_view(con: sqlite3.Connection, mem: int) -> str:
    """这段第一人称叙述里的「我」是谁。她自己讲的，返回空。"""
    try:
        from . import view as view_mod
        who = view_mod.view_of(con, mem)
    except Exception:
        return ""
    return "" if who in ("", "未知", "中心人物") else who


# ── 头段的东西也能被想起：canon 卡走话题键 ──────────────────────────
# 53 张卡在图上是只进不出的叶子（入边 innate、出边 next、元素边 0），
# 三路召回全以元素为入口，所以正问「最后的宴会」，浮上来的是乐土里的叙事段。
# 这里用 `topic` 边上人工/半自动指定的键把卡捞出来——不是 has，不影响 df 与骨干。

def head_mems(con: sqlite3.Connection) -> set[int]:
    """**真在头段上**的那批（不是所有 innate）。

    头段只取前 `常驻取舍.innate` 条（按 innate 边的建立顺序），
    53 张卡都挂着 innate，但其中只有十几条能出现在【我知道】里——
    剩下的正是要能被想起来的那些，别一并排除掉。
    """
    lim = 24
    try:
        from . import rules as rules_mod
        lim = int(rules_mod.head_pick(con).get("innate", 24))
    except Exception:
        pass
    return {int(r["dst"]) for r in con.execute(
        "SELECT dst FROM edges WHERE rel='innate' ORDER BY id LIMIT ?", (lim,))}


def cards_for_text(con: sqlite3.Connection, text: str, limit: int = 1,
                   exclude: set[int] | None = None) -> list[dict]:
    """这句话提到了某张卡的话题键 → 把那张卡也翻出来。

    打分只看两件事：**键有多锐**（人名权重大、df 越罕见越好）和 **键有多长**。
    「乐土」这种谁都能对上的键，权重自然压得下不去。
    """
    if not text:
        return []
    df = {int(r["node_id"]): int(r["df"]) for r in con.execute("SELECT node_id, df FROM derived_df")}
    try:
        from . import view as view_mod
        names = set(view_mod.NAMES)
    except Exception:
        names = {"乙"}
    exclude = exclude if exclude is not None else head_mems(con)
    rows = con.execute(
        "SELECT e.src AS mem, n.body AS key, s.provenance, s.order_key, t.body AS title "
        "FROM edges e JOIN nodes n ON n.id=e.dst "
        "LEFT JOIN mem_src s ON s.mem_node=e.src "
        "LEFT JOIN nodes t ON t.id=e.src "
        "WHERE e.rel='topic'").fetchall()
    best: dict[int, tuple[float, str]] = {}
    for r in rows:
        k = str(r["key"])
        mem = int(r["mem"])
        # 单字人名（苏／樱／华）也是键；非人名的单字词不要
        if mem in exclude or (len(k) < 2 and k not in names) or len(k) < 1:
            continue
        if k in NOISE or k in META or k not in text:
            continue
        # 键越锐越好：人名加成、词越长越具体、df 越大越不稀罕
        rare = min(df.get(_key_id(con, k), 0), 120) / 20.0
        score = (2.0 if k in names else 1.0) * len(k) / (1 + rare)
        # 「丙：关系与语气」比「结局：…（提到丙）」更像是问丙时要的那张
        if str(r["title"] or "").startswith(k):
            score *= 1.6
        if score > best.get(mem, (0.0, ""))[0]:
            best[mem] = (score, k)
    out = []
    for mem, (score, key) in sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]:
        row = con.execute(
            "SELECT n.body AS title, s.provenance, s.first_person, s.raw "
            "FROM nodes n LEFT JOIN mem_src s ON s.mem_node=n.id WHERE n.id=?", (mem,)).fetchone()
        grp = str(row["provenance"] or "").split(":", 1)[-1] if row else ""
        gist = _norm(row["first_person"] or row["raw"] or "") if row else ""
        out.append({"mem": mem, "key": key, "score": round(score, 3), "group": grp,
                    "title": _norm(row["title"]) if row else "", "gist": gist})
    return out


def _key_id(con: sqlite3.Connection, body: str) -> int:
    row = con.execute(
        "SELECT id FROM nodes WHERE body=? AND kind IN ('entity','affect','person') LIMIT 1",
        (body,)).fetchone()
    return int(row["id"]) if row else 0
