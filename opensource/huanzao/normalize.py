"""清洗、切分、第一人称改写。"""
from __future__ import annotations

import re

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_SPACE = re.compile(r"[ \t\u3000]+")
_SPLIT = re.compile(r"[。！？；\n]+")
_SUB = re.compile(r"[，、,]+")
_QUOTED = re.compile(r"[「『“\"《](.+?)[」』”\"》]")


def norm(text: str) -> str:
    if not text:
        return ""
    s = str(text).translate(_ZW)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _SPACE.sub(" ", s)
    return s.strip()


def clauses(text: str) -> list[str]:
    out: list[str] = []
    for part in _SPLIT.split(norm(text)):
        part = part.strip()
        if not part:
            continue
        for piece in _SUB.split(part):
            piece = piece.strip(" 　:：·—-")
            if len(piece) >= 2:
                out.append(piece)
    return out


def quoted(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTED.finditer(norm(text)) if len(m.group(1).strip()) >= 2]


# —— 第一人称改写（rules 版，有损，仅用于离线跑通全链路）——
# 说明：真正要用的第一人称由 LLM 重写；这里只把叙述视角翻过去，不动事实。
_PERSON = [
    ("用户说", "他对我说"),
    ("用户表示", "他对我说"),
    ("用户", "他"),
    ("她", "我"),
    ("中心人物", "我"),
]


def first_person_rules(text: str) -> str:
    s = norm(text)
    for a, b in _PERSON:
        s = s.replace(a, b)
    return "我记得，" + s if s else ""


def cjk_runs(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]+", text)
