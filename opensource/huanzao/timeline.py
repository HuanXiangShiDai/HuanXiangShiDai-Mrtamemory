"""时间判定：把「入库时间」换成「叙事时间」。

资料库里有两个时间：一个是入库时打的戳（2026-11-15），一个是她真正经历的先后。
761 条乐土记忆全挤在同一个入库戳上——按那个切，等于没有先后。

真正的顺序键其实一直在库里：`arc`（ER_ch1/ch2/ch3）+ `source_refs` 的行号区间。

四段人生：

    0 先天设定   她是怎样的人（无先后，是根）
    1 生前回忆   诞生、惨剧、宴会、终局（她自己的事）
    2 乐土       遇见的那些人、那些天（ch1 -> ch2 -> ch3，按行号）
    3 现在       2026 年 11 月，和你说话的日子
"""
from __future__ import annotations

import json
import re
import sqlite3

SETTING, LIFE, LOFT, NOW = 0, 1, 2, 3

_REF = re.compile(r"(ER_ch(\d)_full\.md)#L(\d+)-L(\d+)")
_CH = re.compile(r"^ER_ch(\d)$")
# 现在那段的取号：tab_s_20261115_204049_142079_03 -> 日、会话时刻、段序
_TAB = re.compile(r"_(\d{8})_(\d{6})_\d+_(\d+)$")

# 生前那十几件大事的先后（从 canon 事件表抄下来的顺序，不能靠字母序）
LIFE_ORDER = [
    "canon:origin_cocoon",          # 诞生：从终焉之茧坠落
    "canon:cm002_anomaly",          # 改造战士 CM-002 的三处异常
    "canon:roster_founded",         # 十三英桀的位次是她亲手排的
    "canon:yield_first_seat",       # 让贤
    "canon:binding_catastrophe",    # 约束的惨剧之后
    "canon:twelfth_herrsher_rin",   # 第十二律者：午
    "canon:final_banquet",          # 最后的宴会
    "canon:fake_thirteenth",        # 真相：第十三律者
    "canon:kevin_deliverance",      # 送别
    "canon:sacrifice_gift",         # 她换来的东西
    "canon:elysium_realm",          # 某处旧址
    "canon:guide_mei",              # 引导乙
    "canon:final_dissolve",         # 终局
]


def _life_index(event_id: str) -> int:
    return LIFE_ORDER.index(event_id) if event_id in LIFE_ORDER else 50


def order_key(arc: str | None, event_id: str | None, when: str | None,
              refs: str | None, src_id: str | None = None) -> tuple[int, int, int, int, int, int]:
    """给一条记忆排出它在叙事里的位置。返回六元组，可直接比较。

    (段, 章/人生序号, 行号, 日, 会话时刻×100+段序, 段内序)
    最后两位是同一处的先后：同一段对话里第几句、同一截原文里第几条。
    """
    arc = (arc or "").strip()
    event_id = (event_id or "").strip()
    refs = refs or ""
    src_id = (src_id or "").strip()
    tail = 0
    m_tail = re.search(r"_(\d+)$", src_id)
    if m_tail:
        tail = int(m_tail.group(1))

    # 她自己的那十几件大事：无论被归到哪个 arc，都按人生顺序走
    if event_id in LIFE_ORDER:
        return (LIFE, _life_index(event_id), 0, 0, 0, 0)

    if arc == "设定":
        return (SETTING, 0, 0, 0, 0, tail)

    m = _REF.search(refs)
    if m:
        return (LOFT, int(m.group(2)), int(m.group(3)), 0, 0, tail)

    ch = _CH.match(arc)
    if ch:
        return (LOFT, int(ch.group(1)), 0, 0, 0, tail)

    if event_id.startswith("canon:"):
        if "旧版" in arc:
            return (LOFT, 9, 0, 0, 0, tail)   # 旧版摘要：已被重切版替代，不进生长
        return (LIFE, _life_index(event_id), 0, 0, 0, 0)

    # 现在：细到「哪一天、哪个会话、第几段」——同一天里也有先后
    tab = _TAB.search(src_id)
    if tab:
        day = int(tab.group(1))
        turn = int(tab.group(2)) * 100 + int(tab.group(3))
        return (NOW, 0, 0, day, turn, tail)

    if when and when != "canon":
        digits = re.sub(r"\D", "", when)
        if len(digits) >= 8:
            return (NOW, 0, 0, int(digits[:8]), 0, tail)
        return (NOW, 0, 0, 99999999, 0, tail)   # live：没有日期的，排在最后

    return (NOW, 0, 0, 99999999, 0, tail)


def _key_text(k: tuple[int, int, int, int, int, int]) -> str:
    return f"{k[0]}:{k[1]:03d}:{k[2]:010d}:{k[3]:08d}:{k[4]:010d}:{k[5]:06d}"


def migrate(graph: sqlite3.Connection, src_path: str) -> int:
    """把叙事顺序写进 mem_src.order_key（派生字段，可随时重算）。"""
    try:
        graph.execute("ALTER TABLE mem_src ADD COLUMN order_key TEXT")
        graph.commit()
    except sqlite3.OperationalError:
        pass

    src = sqlite3.connect(f"file:{src_path.replace(chr(92), '/')}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    meta = {}
    for r in src.execute("SELECT id, arc, event_id, event_time, source_refs_json FROM tabs"):
        meta[str(r["id"])] = r

    rows = graph.execute("SELECT mem_node, src_id FROM mem_src").fetchall()
    n = 0
    for r in rows:
        m = meta.get(str(r["src_id"]))
        if not m:
            continue
        k = order_key(m["arc"], m["event_id"], m["event_time"],
                      m["source_refs_json"], r["src_id"])
        graph.execute("UPDATE mem_src SET order_key = ? WHERE mem_node = ?",
                      (_key_text(k), int(r["mem_node"])))
        n += 1
    graph.commit()
    return n


def slices(graph: sqlite3.Connection) -> list[tuple[str, list[int]]]:
    """按叙事时间切片：设定 -> 生前 -> 乐土每章 -> 现在的每一天。"""
    rows = graph.execute(
        "SELECT mem_node, order_key, provenance FROM mem_src WHERE order_key IS NOT NULL "
        "ORDER BY order_key, mem_node"
    ).fetchall()
    groups: dict[str, list[int]] = {}
    for r in rows:
        parts = str(r["order_key"]).split(":")
        seg = parts[0]
        if seg == str(LOFT):
            ch = int(parts[1])
            # 旧图把「旧版摘要」排在 ch9 当哨兵；新图的 ch9 是真章节
            # （主线31致中心人物-01C，20 段），不能按章号一刀切。
            if ch == 9 and str(r["provenance"] or "").startswith("旧版"):
                continue                     # 旧版摘要已被重切版替代，不进生长
            key = f"乐土-ch{ch}"
        elif seg == str(NOW):
            day = parts[3].lstrip("0") or "0"
            if day == "99999999":
                key = "现在-未定"            # 原文里只有「今晚」，没留下日历
            else:
                key = f"现在-{day[:4]}.{day[4:6]}.{day[6:]}" if len(day) == 8 else "现在-未定"
        elif seg == str(LIFE):
            key = "生前"
        else:
            key = "先天设定"
        groups.setdefault(key, []).append(int(r["mem_node"]))
    # 先四段人生，再乐土各章（按章号），最后「现在」按天。
    # 以前把「现在」插在 ch3 后面，ch4-ch10 于是跑到「现在」之后去了。
    head = [(k, groups[k]) for k in ("先天设定", "生前") if k in groups]
    loft = [(k, groups[k]) for k in sorted((k for k in groups if k.startswith("乐土-ch")),
                                           key=lambda k: int(k.split("ch")[1]))]
    nows = [(k, groups[k]) for k in sorted((k for k in groups if k.startswith("现在")))]
    rest = [(k, v) for k, v in groups.items()
            if k not in ("先天设定", "生前") and not k.startswith(("乐土-ch", "现在"))]
    return head + loft + nows + rest


def check(graph: sqlite3.Connection) -> dict:
    """时间轴自检：有没有倒退、有没有缺口、有没有两条撞在同一个位置上。"""
    rows = graph.execute(
        "SELECT mem_node, order_key FROM mem_src WHERE order_key IS NOT NULL "
        "ORDER BY order_key, mem_node").fetchall()
    keys = [str(r["order_key"]) for r in rows]
    dup = len(keys) - len(set(keys))
    back = 0
    for a, b in zip(keys, keys[1:]):
        if b < a:
            back += 1
    missing = graph.execute("SELECT COUNT(*) c FROM mem_src WHERE order_key IS NULL").fetchone()["c"]
    return {"total": len(keys), "duplicate_keys": dup, "backwards": back, "missing": missing}
