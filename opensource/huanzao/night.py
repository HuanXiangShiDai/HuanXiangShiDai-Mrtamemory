"""夜间窗口：凌晨 3–4 点的自发激活。

用户要求：这段时间里脉冲模拟自动激活元素，再由激活触发自指；
**思考可以超时**（窗口是发车时间，不是到站时间）。
激活的目标是四件事：总结、批判、修正、进化。

窗口、目标、以及"允许超时"都写成图里的规则，所以它们可以被改：

    rule|激活窗口    start=03:00；end=04:00；overflow=允许；pulse=simulate
    rule|激活目标    总结、批判、修正、进化

这一层只做**判定与执行**，不装定时器——定时器属于宿主（cron / 插件 setInterval）。
`in_window()` 给宿主问"现在该不该跑"，`run_window()` 跑一轮。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, time

from . import activation, db, rule_graph

WINDOW_RULE = "激活窗口"
TARGETS_RULE = "激活目标"

WINDOW_BODY = ("激活窗口（夜间自发思考）：start=03:00；end=04:00；"
               "overflow=允许；pulse=simulate；note=窗口是发车时间，不是到站时间")
TARGETS_BODY = "激活目标（脉冲要达成的四件事）：总结、批判、修正、进化"


def ensure(con: sqlite3.Connection) -> dict:
    """把窗口与目标建进图。幂等。"""
    made = []
    for name, body in ((WINDOW_RULE, WINDOW_BODY), (TARGETS_RULE, TARGETS_BODY)):
        if not db.get_node(con, "rule|" + name):
            db.node(con, "rule", body, uniq="rule|" + name)
            made.append(name)
    con.commit()
    return {"made": made}


def window(con: sqlite3.Connection) -> dict:
    """读窗口配置（图里的规则，不是代码常量）。"""
    try:
        body = rule_graph.current(con, WINDOW_RULE) or WINDOW_BODY
        kv = dict(rule_graph.parse_body(body).get("kvs", []))
    except Exception:
        kv = {}

    def _t(s, dflt):
        try:
            h, m = str(s).split(":")[:2]
            return time(int(h), int(m))
        except Exception:
            return dflt

    return {"start": _t(kv.get("start", "03:00"), time(3, 0)),
            "end": _t(kv.get("end", "04:00"), time(4, 0)),
            "overflow": kv.get("overflow", "允许") in ("允许", "1", "true", "yes"),
            "raw": kv}


def targets(con: sqlite3.Connection) -> list[str]:
    try:
        body = rule_graph.current(con, TARGETS_RULE) or TARGETS_BODY
        tail = body.split("：", 1)[-1]
        return [x.strip() for x in tail.split("、") if x.strip()]
    except Exception:
        return ["总结", "批判", "修正", "进化"]


def in_window(con: sqlite3.Connection, now: datetime | None = None) -> dict:
    """现在在不在激活窗口里。给宿主问。"""
    w = window(con)
    now = now or datetime.now()
    t = now.time()
    inside = w["start"] <= t < w["end"]
    return {"inside": inside, "now": now.strftime("%H:%M"),
            "start": w["start"].strftime("%H:%M"), "end": w["end"].strftime("%H:%M"),
            "overflow": w["overflow"]}


def run_window(con: sqlite3.Connection, now: datetime | None = None,
               dry_run: bool = False) -> dict:
    """跑一轮夜间激活：脉冲 → 抬元素 → 四个目标各做一件事。

    `dry_run=True` 只报告会做什么。
    窗口只决定"该不该发车"，**不限制跑多久**（overflow=允许）。
    """
    chk = in_window(con, now)
    if not chk["inside"]:
        return {"ran": False, "why": "不在窗口内", **chk}

    tg = targets(con)
    activation.ensure(con)
    # 种子要 node id：`activation.top()` 返回的是正文，不能直接当 id 用
    vals = activation.values(con)
    seeds = [n for n, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:6]]
    if dry_run:
        return {"ran": False, "dry_run": True, "targets": tg, "seeds": seeds, **chk}

    pulse = activation.pulse_round(con, seeds, hops=2)
    report = {"ran": True, "targets": tg, "seeds": pulse.get("seeds", 0),
              "reached": pulse.get("reached", 0), "did": {}, **chk}
    for t in tg:
        fn = ACTIONS.get(t)
        if fn is None:
            report["did"][t] = {"ok": False, "why": "没有对应的执行器"}
            continue
        try:
            report["did"][t] = fn(con)
        except Exception as e:
            report["did"][t] = {"ok": False, "why": str(e)[:80]}
    con.commit()
    return report


# ── 四个目标的执行器 ──────────────────────────────────────────────────
# 它们是"目标"而不是"标签"：每个都做一件具体的事，并回报做了什么。

def _summarize(con: sqlite3.Connection) -> dict:
    """总结：把今天还没收口的思考收成总结（结论顺带落成记忆）。"""
    from . import autoself
    made = 0
    rows = con.execute(
        "SELECT session, turn, step FROM scratch "
        "GROUP BY session, turn, step LIMIT 20").fetchall()
    for r in rows:
        got = autoself.digest(con, str(r["session"]), int(r["turn"]), int(r["step"]))
        if got.get("digest"):
            made += 1
    return {"ok": True, "close_cases": made}


def _criticize(con: sqlite3.Connection) -> dict:
    """批判：找"结论与前提规则冲突"的地方——结论说要收紧，规则却放宽了。"""
    from . import rule_graph
    bad = []
    for r in con.execute(
            "SELECT n.id, n.body FROM nodes n JOIN mem_src s ON s.mem_node=n.id "
            "WHERE s.provenance='thought-conclusion' LIMIT 50").fetchall():
        body = str(r["body"])
        refs = [str(x["uniq"]) for x in con.execute(
            "SELECT nb.uniq FROM edges e JOIN nodes nb ON nb.id=e.dst "
            "WHERE e.src=? AND e.rel='refers'", (int(r["id"]),))]
        if not refs:
            continue
        tighten = any(w in body for w in ("收紧", "少记", "少留", "减少"))
        loosen = any(w in body for w in ("放宽", "多记", "多留", "增加"))
        for u in refs:
            if "|v" not in u:
                continue
            info = con.execute("SELECT body FROM nodes WHERE uniq=?", (u,)).fetchone()
            if not info:
                continue
            nb = str(info["body"])
            if tighten and any(k in nb for k in ("innate=3", "innate=4")):
                bad.append({"结论": body[:40], "规则": u, "冲突": "结论要收紧，规则却放宽"})
            elif loosen and "innate=2" in nb:
                bad.append({"结论": body[:40], "规则": u, "冲突": "结论要放宽，规则却收紧"})
    return {"ok": True, "conflicts": len(bad), "examples": bad[:3]}


def _correct(con: sqlite3.Connection) -> dict:
    """修正：把批判结果里能自动修的记成提案，不硬改（改要走 C2）。"""
    got = _criticize(con)
    if not got.get("conflicts"):
        return {"ok": True, "proposed": 0}
    proposals = 0
    for ex in got.get("examples", []):
        name = str(ex["规则"]).split("|")[1]
        try:
            from . import persona
            persona.propose(con, "sum|night|0|0", "canon|tab_canon_core_persona", -1,
                            why="夜间批判：%s" % ex["冲突"][:40])
            proposals += 1
        except Exception:
            continue
    return {"ok": True, "proposed": proposals, "note": "只提案，不硬改"}


def _evolve(con: sqlite3.Connection) -> dict:
    """进化：统计自指深度与规则版本，给下一轮留一份读数（不改任何东西）。"""
    from . import rule_c2
    d = rule_c2.depth(con)
    versions = con.execute(
        "SELECT COUNT(*) c FROM nodes WHERE kind='rule' AND uniq LIKE '%|v%'").fetchone()["c"]
    return {"ok": True, "delta_star": d.get("δ*"), "level": d.get("L"),
            "rule_versions": int(versions),
            "note": "这只是读数；不为了让它变大而改任何东西"}


ACTIONS = {
    "总结": _summarize,
    "批判": _criticize,
    "修正": _correct,
    "进化": _evolve,
}
