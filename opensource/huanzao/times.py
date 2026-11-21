"""时间语当元素——精确到秒。

参照 **TimeML / ISO-TimeML**：时间表达是有类型的实体（TIMEX），
相对时间按「说话时刻」归一，时刻则补齐到 **秒**。

落到图里两种点：

- `kind='time'`   正文 `2026-11-15T20:41:52` —— 精确时刻
- `kind='day'`    正文 `2026-11-15`          —— 只到日

记忆用 `at` 边指时刻、用 `when` 边指日期。只说了「那天」的，连到日；
说了「晚上快九点」的，两头都连——日用来找范围，时刻用来排序。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from . import db

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
REL_DAYS = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2, "大前天": -3,
            "明天": 1, "后天": 2}
_REL_WEEK = {"上周": -7, "上星期": -7, "这周": 0, "本周": 0, "下周": 7, "下星期": 7}
PERIOD = [("凌晨", 0), ("早上", 0), ("清晨", 0), ("上午", 0), ("中午", 0),
          ("下午", 0), ("傍晚", 0), ("晚上", 0), ("夜里", 0)]


def _cn2int(s: str) -> int | None:
    """一 / 十 / 十一 / 二十三 → 阿拉伯数字。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        tail = CN_DIGIT.get(s[1:], None)
        return 10 + tail if tail is not None else None
    if "十" in s:
        a, _, b = s.partition("十")
        tens = CN_DIGIT.get(a, 0)
        unit = CN_DIGIT.get(b, 0) if b else 0
        return tens * 10 + unit
    total = 0
    for ch in s:
        d = CN_DIGIT.get(ch)
        if d is None:
            return None
        total = total * 10 + d
    return total


def _apply_period(hour: int, text: str) -> int:
    if "凌晨" in text and hour == 12:
        return 0
    if any(w in text for w in ("下午", "傍晚", "晚上", "夜里")) and hour < 12:
        return hour + 12
    if "中午" in text and hour < 11:
        return hour + 12
    return hour


def _clock(text: str) -> tuple[int, int, int] | None:
    """从文字里找时刻，返回 (时, 分, 秒)。找不到返回 None。"""
    # 1) 数字形式：21:05 / 21：05：30
    m = re.search(r"(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2}))?", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        s = int(m.group(3) or 0)
        return (_apply_period(h, text), min(mi, 59), min(s, 59))

    # 2) 中文/混合：九点 / 九点半 / 九点零五分 / 一点五十五 / 8点多 / 快九点
    m = re.search(r"([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[点时]", text)
    if not m:
        return None
    hour = _cn2int(m.group(1))
    if hour is None or not (0 <= hour <= 24):
        return None
    rest = text[m.end():m.end() + 6]
    minute, second = 0, 0
    if rest.startswith("半"):
        minute = 30
    elif rest.startswith("一刻"):
        minute = 15
    elif rest.startswith("三刻"):
        minute = 45
    else:
        m2 = re.match(r"\s*(零|〇)?\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*分?", rest)
        if m2 and m2.group(2):
            v = _cn2int(m2.group(2))
            if v is not None and 0 <= v <= 59:
                minute = v
    # 「快九点」= 还差几分到九点 → 8:55，不是 9:55
    nearly = "快" in text[max(0, m.start() - 2):m.start()] or \
             "差" in text[max(0, m.start() - 2):m.start()]
    if nearly and minute == 0:
        minute = 55
        bump = -1
    m3 = re.search(r"(\d{1,2})\s*秒", text)
    if m3:
        second = min(int(m3.group(1)), 59)
    hour = _apply_period(hour, text)
    if nearly and minute == 55:
        hour -= 1          # 「快九点」落在八点多
    if hour < 0:
        hour += 24
    if hour == 24:
        hour = 0
    return (max(0, min(hour, 23)), minute, second)


def parse(text: str, base: datetime | None = None) -> list[dict]:
    """返回 [{'day': '2026-11-15', 'at': '2026-11-15T20:41:52'|None, 'why': '…'}]"""
    base = base or datetime.now()
    today = base.date()
    days: list[date] = []

    def add(d: date) -> None:
        if d not in days:
            days.append(d)

    for m in re.finditer(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text):
        try:
            add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    if not days:
        for m in re.finditer(r"(\d{1,2})月(\d{1,2})[日号]", text):
            try:
                add(date(today.year, int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass
    if not days:
        # 中文数字：十一月十七号 / 三号 —— 全是阿拉伯数字的语料里没人这么写，
        # 可他一开口就是这么说的
        for m in re.finditer(r"([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})[日号]", text):
            mo, dy = _cn2int(m.group(1)), _cn2int(m.group(2))
            if mo and dy:
                try:
                    add(date(today.year, mo, dy))
                except ValueError:
                    pass
    if not days:
        for word, delta in REL_DAYS.items():
            if word in text:
                add(today + timedelta(days=delta))
                break
    if not days:
        for word, delta in _REL_WEEK.items():
            if word in text:
                m = re.search(word + r"([一二三四五六日天])", text)
                if m:
                    idx = "一二三四五六日天".index(m.group(1))
                    idx = 6 if idx == 7 else idx
                    monday = today - timedelta(days=today.weekday())
                    add(monday + timedelta(days=delta + idx))
                break

    clock = _clock(text)
    if not days:
        if clock is None:
            return []
        add(today)          # 只说了钟点，归到当天

    out = []
    for d in days:
        at = None
        if clock is not None:
            at = datetime(d.year, d.month, d.day, clock[0], clock[1], clock[2]).isoformat()
        out.append({"day": d.strftime("%Y-%m-%d"), "at": at})
    return out


def node_for(con: sqlite3.Connection, day: str) -> int:
    return db.node(con, "day", day, uniq=f"day|{day}")


def at_node(con: sqlite3.Connection, iso: str) -> int:
    return db.node(con, "time", iso, uniq=f"time|{iso}")


def is_now(con: sqlite3.Connection, mem: int) -> bool:
    """只有「现在」那段用日历；往事用叙事坐标（段:章:行号），没有日历可对。"""
    row = con.execute("SELECT order_key FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    if not row or not row["order_key"]:
        return False
    return str(row["order_key"]).startswith("3:")


def own_day(con: sqlite3.Connection, mem: int) -> str:
    """这条记忆**自己发生在哪天**——看时间键的日字段，不看正文里提到的日子。"""
    row = con.execute("SELECT order_key, when_iso FROM mem_src WHERE mem_node=?", (mem,)).fetchone()
    if not row:
        return ""
    key = str(row["order_key"] or "")
    parts = key.split(":")
    if len(parts) >= 4 and parts[3].isdigit() and parts[3] != "00000000":
        d = parts[3]
        if d != "99999999":
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    iso = str(row["when_iso"] or "")
    if re.match(r"20\d{2}-\d{2}-\d{2}", iso):
        return iso[:10]
    return ""


def assign_own(con: sqlite3.Connection, mem: int) -> str:
    day = own_day(con, mem)
    if day:
        db.edge(con, mem, node_for(con, day), "when")
    return day


def _precision(con: sqlite3.Connection) -> str:
    """时刻记到多细——从图的「时间坐标」规则读（秒 / 分 / 日）。"""
    try:
        from . import rules as rules_mod
        return str(rules_mod.time_rule(con).get("now_precision", "秒") or "秒")
    except Exception:
        return "秒"


def _cut(iso: str, precision: str) -> str:
    if "分" in precision:
        return iso[:16] + ":00" if len(iso) >= 16 else iso
    if "日" in precision:
        return iso[:10]
    return iso


def link(con: sqlite3.Connection, mem: int, text: str, base: datetime | None = None) -> list[dict]:
    """正文里提到的时间 → **事件时间**（rel='mentions-time'），不是这条记忆的日期。"""
    if not is_now(con, mem):
        return []
    got = parse(text, base)
    precision = _precision(con)
    for item in got:
        db.edge(con, mem, node_for(con, item["day"]), "mentions-time")
        if item["at"]:
            db.edge(con, mem, at_node(con, _cut(item["at"], precision)), "mentions-time")
    return got
