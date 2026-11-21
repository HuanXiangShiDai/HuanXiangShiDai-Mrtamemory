"""说话之前先判一个「框架」。

每句话进来，先问三件事：

1. **这是哪一类话**——问往事、交代事实、说计划、吐情绪、谈系统、闲聊；
2. **时间落在哪天**（交给 `times`，归一成日期）；
3. **因果角色**——他在开场、在补充、在纠错、还是在收尾。

学术上这叫框架语义：**FrameNet** 把「一句话激活了哪个语义框架」当作理解的第一步；
**PropBank / SRL** 则标注「谁对谁做了什么」。我们只取最粗的一层——
因为这一层足够决定「该不该翻记忆、翻哪段时间的记忆」。

规则版先跑通：确定性、零成本、可解释。模型版以后再加。
"""
from __future__ import annotations

import re
from datetime import datetime

from . import times

FRAMES = ("问往事", "说事实", "说计划", "吐情绪", "谈系统", "闲聊")

_ASK = ("记得", "还记得", "上次", "之前", "那天", "当时", "以前", "是不是说过", "你说过")
_FACT = ("其实", "是说", "我的意思", "换句话说", "也就是说", "不是这样", "纠正")
_PLAN = ("要不要", "明天", "待会", "接下来", "计划", "准备", "先", "然后")
_EMO = ("难过", "开心", "累", "烦", "气", "怕", "想你", "舍不得", "高兴", "难受")
# 「气」这种单字会误中「天气」「空气」——按上下文挡掉
_EMO_GUARD = {"气": ("天", "空", "暖", "语", "力", "神", "客", "运")}
_META = ("记忆", "元图", "元素", "上下文", "规则", "插件", "系统", "接口", "缓存", "模型")


def judge(text: str, base: datetime | None = None) -> dict:
    """判框架：返回 {frame, days, role, why}。"""
    t = text.strip()
    parsed = times.parse(t, base)
    days = [x["day"] for x in parsed]          # 只要日期，时刻另算
    ats = [x["at"] for x in parsed if x.get("at")]
    why = []

    def hit(words):
        got = []
        for w in words:
            i = t.find(w)
            while i >= 0:
                guard = _EMO_GUARD.get(w)
                if not guard or i == 0 or t[i - 1] not in guard:
                    got.append(w)
                    break
                i = t.find(w, i + 1)
        return got

    score = {f: 0 for f in FRAMES}
    for w in hit(_ASK):
        score["问往事"] += 2
        why.append(f"含「{w}」")
    for w in hit(_EMO):
        score["吐情绪"] += 2
        why.append(f"含情绪词「{w}」")
    for w in hit(_META):
        score["谈系统"] += 1
        why.append(f"含机制词「{w}」")
    for w in hit(_FACT):
        score["说事实"] += 1
    for w in hit(_PLAN):
        score["说计划"] += 1
    if days:
        # 有时间不等于要翻旧账——「今天天气不错」也带时间。
        # 时间只作为**过滤器**用在问往事上，不单独加分。
        why.append(f"带时间「{days[0]}」")

    frame = max(score, key=lambda f: score[f])
    if score[frame] == 0:
        frame = "闲聊"
        why.append("没命中任何线索")

    # 因果角色：开场／补充／纠错／收尾
    role = "陈述"
    if re.search(r"^(好|嗯|行|可以|ok)", t, re.I):
        role = "收尾"
    elif hit(("不对", "不是", "等等", "我的意思", "纠正")):
        role = "纠错"
    elif hit(("还有", "另外", "顺便", "补充")):
        role = "补充"
    elif t.endswith(("？", "?")):
        role = "发问"

    return {"frame": frame, "days": days, "ats": ats, "role": role, "why": why or ["无"]}


def plan(text: str, base: datetime | None = None) -> dict:
    """给检索用：这一句该不该翻、翻哪段。"""
    got = judge(text, base)
    got["should_recall"] = got["frame"] in ("问往事", "吐情绪")
    return got


def mems_on(con, days: list[str]) -> set[int]:
    """哪些记忆落在这几天——时间当**过滤器**用：先缩范围，再找元素。"""
    if not days:
        return set()
    out: set[int] = set()
    for day in days:
        row = con.execute("SELECT id FROM nodes WHERE kind='day' AND body=?", (day,)).fetchone()
        if not row:
            continue
        for r in con.execute("SELECT DISTINCT src FROM edges WHERE dst=? AND rel IN ('when','at')",
                             (int(row["id"]),)).fetchall():
            out.add(int(r["src"]))
    return out
