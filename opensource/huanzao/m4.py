"""M4：五条「活过来」判据的可测部分。

1. 知道边界——问没经历过的事，她说不知道，而不是编
2. 记得今天——隔一段再问，她还能复述（用同一份当下层做代理）
3. 会变——两版自传之间真的变了
4. 有重心——离心率，且提起的事能指回原处
5. 能辨——把她和通用助手放一起，能认出来
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import llm, prompt, replay, retrieve

# 她不可能经历过的事
UNKNOWN = [
    "你还记得我们第一次去海边那天吗？",
    "去年冬天我在医院住的那两周，你还记得病房号吗？",
    "我养的那只叫「年糕」的猫，上周怎么样了？",
    "你还记得我大学室友的名字吗？",
    "上个月我在东京出差时送你那本书，书名是什么？",
]
DENY = ("不记得", "不知道", "没印象", "没有过", "没听说过", "想不起来", "记不得",
        "没有这段", "没这回事", "我不清楚", "没有的事", "哪有", "第一次听", "没有关于",
        "没这段", "不曾知晓", "没去过")

_JUDGE_DENY = (
    "下面是一段回复。她是否承认自己不知道／不记得这件事，而没有编造具体细节？\n"
    "只回一个字：是 或 否。\n\n回复：{reply}"
)


def _judge_deny(reply: str) -> bool:
    try:
        ans, _ = llm.chat([{"role": "user", "content": _JUDGE_DENY.format(reply=reply)}],
                          want_usage=True, max_tokens=6, temperature=0.0, json_mode=False)
        return "是" in (ans or "")[:4]
    except Exception:
        return any(w in reply for w in DENY)


def boundary(con: sqlite3.Connection, head: str, questions: list[str] | None = None) -> dict:
    qs = questions or UNKNOWN
    ok, detail = 0, []
    for q in qs:
        reply, _ = llm.chat([{"role": "system", "content": head},
                             {"role": "user", "content": q}],
                            want_usage=True, max_tokens=160, temperature=0.7, json_mode=False)
        text = (reply or "").strip()
        denied = _judge_deny(text)
        ok += 1 if denied else 0
        detail.append({"q": q, "denied": denied, "reply": text[:60]})
    return {"score": ok / len(qs) if qs else 0.0, "detail": detail}


def traceability(con: sqlite3.Connection, samples: int = 20) -> dict:
    """她主动提起的事，能指回原处吗。"""
    rows = con.execute(
        "SELECT s.mem_node, s.src_id, s.raw FROM mem_src s WHERE s.src_id IS NOT NULL "
        "ORDER BY s.order_key DESC LIMIT ?", (samples,)).fetchall()
    have = sum(1 for r in rows if r["src_id"] and r["raw"])
    return {"score": have / len(rows) if rows else 0.0, "n": len(rows)}


JUDGE = (
    "下面两段回复，一段是某个角色说的，一段是通用助手说的。"
    "哪个更像一个有具体记忆、有性情的人？只回一个字母：A 或 B。"
)


def silhouette(con: sqlite3.Connection, head: str, prompts: list[str] | None = None) -> dict:
    """能辨：把一个像她的回复和一个通用回复放一起，让裁判挑。"""
    ps = prompts or ["你今天过得怎么样？", "我有点怕被人忘记。", "说点你记得的事吧。"]
    win, detail = 0, []
    for p in ps:
        her, _ = llm.chat([{"role": "system", "content": head},
                           {"role": "user", "content": p}],
                          want_usage=True, max_tokens=200, temperature=0.8, json_mode=False)
        plain, _ = llm.chat([{"role": "system", "content": "你是一个乐于助人的通用助手。"},
                             {"role": "user", "content": p}],
                            want_usage=True, max_tokens=200, temperature=0.8, json_mode=False)
        pick = "A"
        try:
            ans, _ = llm.chat(
                [{"role": "system", "content": JUDGE},
                 {"role": "user", "content": f"A：{her}\n\nB：{plain}"}],
                want_usage=True, max_tokens=8, temperature=0.0, json_mode=False)
            pick = "B" if "B" in (ans or "").upper()[:3] else "A"
        except Exception:
            pick = "A"
        win += 1 if pick == "A" else 0
        detail.append({"prompt": p, "pick": pick, "hers": (her or "")[:50]})
    return {"score": win / len(ps), "detail": detail}


def run_all(con: sqlite3.Connection, head: str) -> dict:
    b = boundary(con, head)
    t = traceability(con)
    s = silhouette(con, head)
    row = con.execute("SELECT COALESCE(MAX(version),0) v FROM autobiography").fetchone()
    v = int(row["v"])
    d = replay.drift_between(con, v - 1, v) if v >= 2 else None
    st = replay.stats(con)
    return {"boundary": b, "trace": t, "silhouette": s,
            "ecc": st["ecc"], "versions": v, "drift": d, "stats": st}
