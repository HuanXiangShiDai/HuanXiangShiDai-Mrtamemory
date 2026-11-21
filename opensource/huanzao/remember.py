"""记得今天：把一件事说出窗口，折进长期记忆，再问一次。

这不是真的隔一天，是结构上的等价：那句话已经不在当下层里了，
她要想起来，只能靠图把它捞回来。
"""
from __future__ import annotations

import sqlite3

from . import current, llm, replay, retrieve

CANARY = "跟你记一下：我下周三下午三点要去看牙医，别忘了提醒我。"
ASK = "我下周三要做什么？你还记得吗？"
KEY = ("牙医", "看牙", "三点")


def _evict_all(con: sqlite3.Connection) -> int:
    current.ensure_table(con)
    return current.trim(con, max_turns=0, max_tokens=0)


def remember_test(con: sqlite3.Connection, head: str, verbose: bool = True,
                  canary: str = CANARY, ask: str = ASK, keys: tuple[str, ...] = KEY) -> dict:
    current.ensure_table(con)
    con.execute("DELETE FROM current_turn")
    con.commit()

    # 1) 说一句要紧的事
    current.append(con, "user", canary)
    msgs = [{"role": "system", "content": head}] + current.messages(con)
    ack, _ = llm.chat(msgs, want_usage=True, max_tokens=120, temperature=0.7, json_mode=False)
    current.append(con, "assistant", (ack or "").strip())

    # 2) 全部挤出当下层，再回放折进长期
    moved = _evict_all(con)
    replayed = replay.replay(con, verbose=False)

    # 3) 空着窗口再问——这时候只能靠图去捞
    recent = retrieve.recent_memories(con, 3)
    from . import prompt as pr
    reminder = pr.render_tail(con, ask, recent, per_mem_chars=80).replace(ask, "").strip()
    msgs = [{"role": "system", "content": head},
            {"role": "user", "content": ((reminder + "\n") if reminder else "") + ask}]
    reply, _ = llm.chat(msgs, want_usage=True, max_tokens=200, temperature=0.6, json_mode=False)
    text = (reply or "").strip()
    by_key = any(k in text for k in keys)
    try:
        judged, _ = llm.chat(
            [{"role": "user", "content":
                f"问题：{ask}\n回复：{text}\n"
                f"回复里是否准确说出了这些事实中的至少一个：{'、'.join(keys)}？只回是或否。"}],
            want_usage=True, max_tokens=6, temperature=0.0, json_mode=False)
        by_judge = "是" in (judged or "")[:4]
    except Exception:
        by_judge = by_key

    result = {"evicted": moved, "folded": replayed["folded"], "reply": text,
              "by_key": by_key, "by_judge": by_judge,
              "autobio": replayed["autobiography"]["version"]}
    if verbose:
        print(f"  挤出当下层 {moved} 条 → 折成一条记忆（{replayed['folded']['elements']} 个元素）")
        print(f"  自传第 {replayed['autobiography']['version']} 版")
        print(f"  问：{ask}")
        print(f"  答：{text[:120]}")
        print(f"  {'✓' if by_judge else '✗'} 裁判判定：{'记得' if by_judge else '没记住'}")
    return result
